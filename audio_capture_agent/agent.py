"""
AudioCaptureAgent — Production Desktop Audio Capture Agent
Captures system audio (WASAPI loopback / Stereo Mix) and streams to backend.

Architecture:
  - Finds loopback/stereo mix device automatically (no user input)
  - Streams PCM audio via WebSocket to /ws/stream
  - Runs a lightweight aiohttp health server on 127.0.0.1:47291 (diagnostics only)
  - Parses protocol args for dynamic WSS URL
  - Saves last connected server to %APPDATA% for auto-reconnect
  - Implements exponential-backoff reconnect + explicit heartbeat ping/pong
  - Strict Singleton execution via Windows Mutex
  - Exits cleanly on SIGINT / SIGTERM
  - Lock-Free Latest Frame Buffer with Thread Decoupling
  - Supervisor model with disposable per-stream runtime instances.

Build:
  pyinstaller --onefile --noconsole --name AudioCaptureAgent --icon icon.ico agent.py
"""

import asyncio
import logging
import os
import signal
import sys
import time
import threading
import uuid
import socket
import json
from urllib.parse import urlparse, parse_qs
from enum import Enum

import ssl
import numpy as np
import pyaudio
import websockets
import orjson

try:
    from aiohttp import web
    from aiohttp.web_middlewares import middleware
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    import win32event
    import win32api
    import winerror
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

# ── Logging (file only — no stdout spam in packaged exe) ───────────────────────
LOG_FILE = os.path.join(os.path.expanduser("~"), "AudioCaptureAgent.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("AudioCaptureAgent")

# ── Configuration (all overridable via env vars) ───────────────────────────────
WS_URL      = os.getenv("AUDIO_ANALYZER_WS", None)
SAMPLE_RATE = int(os.getenv("AUDIO_ANALYZER_RATE",    "48000"))
CHANNELS    = int(os.getenv("AUDIO_ANALYZER_CHANNELS", "2"))
PACKET_SIZE = int(os.getenv("AUDIO_ANALYZER_PACKET",   "1024"))
RMS_THRESHOLD = 100
OFFSET        = 2048
HEALTH_PORT   = int(os.getenv("AUDIO_ANALYZER_HEALTH_PORT", "47291"))
VERSION       = "2.0.0" 
AGENT_INSTANCE_ID = str(uuid.uuid4())

HEARTBEAT_INTERVAL = 15

CONFIG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'AudioAnalyzer')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            log.error("Failed to load config: %s", e)
    return {}

def save_config(config):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f)
    except Exception as e:
        log.error("Failed to save config: %s", e)

def parse_protocol_args():
    server_origin = None
    mode = "idle"
    for arg in sys.argv[1:]:
        if arg.startswith("audioanalyzer://"):
            parsed = urlparse(arg)
            params = parse_qs(parsed.query)
            if "server" in params:
                origin = params["server"][0]
                ws_scheme = "wss" if origin.startswith("https") else "ws"
                host = origin.split("://", 1)[1].rstrip("/")
                server_origin = f"{ws_scheme}://{host}/ws/stream"
            if "mode" in params:
                mode = params["mode"][0]
    return server_origin, mode

# ── Global State & State Machine ───────────────────────────────────────────────

class AgentState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    STREAMING = "streaming"
    STOPPING = "stopping"

agent_state = AgentState.IDLE
lifecycle_lock = asyncio.Lock()
_shutdown = asyncio.Event()

current_runtime = None
desired_streaming = False

class StreamRuntime:
    def __init__(self):
        self.stream_instance_id = str(uuid.uuid4())
        self.session_id = None
        self.token = None
        
        self.websocket = None
        self.audio_interface = None
        self.audio_stream = None
        
        self.capture_thread = None
        self.sender_task = None
        self.heartbeat_task = None
        self.recv_task = None
        self.stop_event = threading.Event()
        
        self.latest_frame = None
        self.frame_version = 0
        self.frame_lock = threading.Lock()
        
        self.dropped_packets = 0
        self.capture_fps_counter = 0
        self.send_fps_counter = 0
        self.dropped_fps_counter = 0
        self.packet_sequence = 0

        # Event + loop reference used to wake sender_loop from the capture thread.
        # asyncio.Event must be signaled via loop.call_soon_threadsafe when
        # called from a non-async thread (Python 3.10+ compatible).
        self.new_frame_event: asyncio.Event = None
        self.event_loop = None  # set in sender_loop when the async loop is known

def _handle_signal(sig, frame):
    log.info("Shutdown signal received (%s).", sig)
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(_shutdown.set)
    except RuntimeError:
        pass

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP Control Server (aiohttp) - Diagnostics ONLY
# ══════════════════════════════════════════════════════════════════════════════

@middleware
async def cors_middleware(request, handler):
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = 'http://127.0.0.1'
    response.headers['Access-Control-Allow-Methods'] = 'GET'
    return response

async def handle_status(request):
    runtime_id = current_runtime.stream_instance_id if current_runtime else None
    ws_conn = current_runtime.websocket is not None if current_runtime else False
    cap_alive = current_runtime.capture_thread.is_alive() if current_runtime and current_runtime.capture_thread else False
    sender_alive = current_runtime.sender_task is not None and not current_runtime.sender_task.done() if current_runtime else False
    heartbeat_alive = current_runtime.heartbeat_task is not None and not current_runtime.heartbeat_task.done() if current_runtime else False
    
    return web.json_response({
        "status": "ok",
        "running": True,
        "streaming": desired_streaming,
        "state": agent_state.value,
        "runtime_id": runtime_id,
        "ws_connected": ws_conn,
        "capture_thread_alive": cap_alive,
        "sender_task_alive": sender_alive,
        "heartbeat_task_alive": heartbeat_alive,
        "session_id": current_runtime.session_id if current_runtime else None,
        "version": VERSION,
        "instance_id": AGENT_INSTANCE_ID
    })

async def handle_start(request):
    global desired_streaming
    desired_streaming = True
    log.info("Received /start command from IPC")
    asyncio.create_task(restart_stream())
    return web.json_response({"status": "starting"})

async def handle_stop(request):
    global desired_streaming
    desired_streaming = False
    log.info("Received /stop command from IPC")
    asyncio.create_task(stop_stream())
    return web.json_response({"status": "stopping"})

async def _start_health_server():
    if not AIOHTTP_AVAILABLE:
        log.warning("aiohttp not available — health endpoint disabled.")
        return None
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get('/status', handle_status)
    app.router.add_get('/start', handle_start)
    app.router.add_post('/start', handle_start)
    app.router.add_get('/stop', handle_stop)
    app.router.add_post('/stop', handle_stop)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', HEALTH_PORT)
    await site.start()
    log.info("HTTP diagnostics server started on http://127.0.0.1:%d/status", HEALTH_PORT)
    return runner


# ══════════════════════════════════════════════════════════════════════════════
# Device Selection
# ══════════════════════════════════════════════════════════════════════════════

_LOOPBACK_KEYWORDS = [
    "stereo mix", "what u hear", "loopback", "wave out mix", "output mix",
    "record what you hear", "virtual audio cable", "vb-audio", "blackhole",
]

def find_loopback_device(audio: pyaudio.PyAudio) -> int:
    best_idx  = -1
    best_name = ""
    for i in range(audio.get_device_count()):
        try:
            info = audio.get_device_info_by_index(i)
        except Exception:
            continue
        if info.get("maxInputChannels", 0) < 1:
            continue
        name_lower = info["name"].lower()
        for kw in _LOOPBACK_KEYWORDS:
            if kw in name_lower:
                best_idx  = i
                best_name = info["name"]
                log.info("Loopback device found: [%d] %s", i, info["name"])
                break
        if best_idx != -1:
            break
    if best_idx == -1:
        try:
            default = audio.get_default_input_device_info()
            best_idx  = int(default["index"])
            best_name = default["name"]
            log.warning("No loopback device found. Falling back to default input: [%d] %s", best_idx, best_name)
        except Exception as exc:
            log.error("Cannot find any input device: %s", exc)
            best_idx = 0
    return best_idx


# ══════════════════════════════════════════════════════════════════════════════
# PCM Normalisation
# ══════════════════════════════════════════════════════════════════════════════

def process_samples(raw: np.ndarray):
    samples = raw.astype(np.float32)
    samples /= 32768.0
    return samples


# ══════════════════════════════════════════════════════════════════════════════
# Streaming Coroutines and Loops
# ══════════════════════════════════════════════════════════════════════════════

async def ws_recv_loop(runtime: StreamRuntime):
    local_gen = runtime.stream_instance_id
    try:
        while not runtime.stop_event.is_set() and not _shutdown.is_set():
            if current_runtime is None or current_runtime.stream_instance_id != local_gen:
                return
            if runtime.websocket:
                await runtime.websocket.recv()
            else:
                break
    except Exception as e:
        log.warning("[runtime=%s] WS read error or closed: %s", local_gen, e)
    finally:
        log.info("[runtime=%s] WS recv loop ended", local_gen)


async def heartbeat_loop(runtime: StreamRuntime):
    local_gen = runtime.stream_instance_id
    while not runtime.stop_event.is_set() and not _shutdown.is_set():
        if current_runtime is None or current_runtime.stream_instance_id != local_gen:
            log.info("[runtime=%s] Heartbeat loop terminating (invalidated).", local_gen)
            return

        if runtime.websocket:
            try:
                await runtime.websocket.send(orjson.dumps({
                    "type": "heartbeat",
                    "timestamp": time.time(),
                    "agent_instance_id": AGENT_INSTANCE_ID
                }).decode('utf-8'))
            except Exception as e:
                log.warning("[runtime=%s] Heartbeat failed: %s", local_gen, e)
                return
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def capture_loop(runtime: StreamRuntime, device_index: int):
    local_gen = runtime.stream_instance_id
    log.info("[runtime=%s] Capture thread started", local_gen)
    
    try:
        while not runtime.stop_event.is_set() and not _shutdown.is_set():
            if current_runtime is None or current_runtime.stream_instance_id != local_gen:
                log.info("[runtime=%s] Capture thread terminating (invalidated).", local_gen)
                break

            try:
                raw_bytes = runtime.audio_stream.read(PACKET_SIZE, exception_on_overflow=False)
            except OSError as e:
                log.error("[runtime=%s] audio read error: %s", local_gen, e)
                break
            except Exception as e:
                log.error("[runtime=%s] audio read exception: %s", local_gen, e)
                break

            capture_timestamp = time.time()
            raw = np.frombuffer(raw_bytes, dtype=np.int16)
            raw = raw.reshape(-1, CHANNELS).mean(axis=1)

            if len(raw) != PACKET_SIZE:
                continue

            samples = process_samples(raw)

            # Store numpy array directly — avoids .tolist() blocking the capture thread.
            # Conversion to list happens on the async sender side.
            with runtime.frame_lock:
                runtime.latest_frame = (samples, capture_timestamp)
                runtime.frame_version += 1
            runtime.capture_fps_counter += 1

            # Wake the sender immediately — eliminates the 5ms polling sleep lag.
            if runtime.new_frame_event is not None and runtime.event_loop is not None:
                try:
                    runtime.event_loop.call_soon_threadsafe(runtime.new_frame_event.set)
                except Exception:
                    pass
    except Exception as e:
        log.error("[runtime=%s] Capture loop error: %s", local_gen, e)
    
    log.info("[runtime=%s] Capture thread exited", local_gen)


async def sender_loop(runtime: StreamRuntime):
    local_gen = runtime.stream_instance_id
    log.info("[runtime=%s] Sender loop started", local_gen)

    # Create the event and capture the running loop — both needed for
    # thread-safe signaling from the sync capture thread.
    runtime.new_frame_event = asyncio.Event()
    runtime.event_loop = asyncio.get_running_loop()

    last_sent_version = 0
    last_log_time = time.time()

    try:
        while not runtime.stop_event.is_set() and not _shutdown.is_set():
            if current_runtime is None or current_runtime.stream_instance_id != local_gen:
                log.info("[runtime=%s] Sender loop terminating (invalidated).", local_gen)
                return

            now = time.time()
            if now - last_log_time >= 2.0:
                ws_buf = runtime.websocket.transport.get_write_buffer_size() if runtime.websocket and hasattr(runtime.websocket.transport, 'get_write_buffer_size') else 0
                log.info("[runtime=%s] CAPTURE FPS: %.1f | SEND FPS: %.1f | DROPPED FPS: %.1f | WS BUFFER: %d bytes | TOTAL DROPS: %d",
                         local_gen,
                         runtime.capture_fps_counter / 2.0,
                         runtime.send_fps_counter / 2.0,
                         runtime.dropped_fps_counter / 2.0,
                         ws_buf,
                         runtime.dropped_packets)
                runtime.capture_fps_counter = 0
                runtime.send_fps_counter = 0
                runtime.dropped_fps_counter = 0
                last_log_time = now

            with runtime.frame_lock:
                current_version = runtime.frame_version
                current_frame_data = runtime.latest_frame

            if current_version > last_sent_version and current_frame_data is not None:
                ws_buf = runtime.websocket.transport.get_write_buffer_size() if runtime.websocket and hasattr(runtime.websocket.transport, 'get_write_buffer_size') else 0
                if ws_buf > 65536:
                    runtime.dropped_packets += 1
                    runtime.dropped_fps_counter += 1
                    last_sent_version = current_version
                    # Don't sleep here — immediately check next event signal
                    continue

                samples_arr, capture_timestamp = current_frame_data

                # Convert numpy → list here on the async thread (not capture thread)
                samples_list = samples_arr.tolist() if hasattr(samples_arr, 'tolist') else samples_arr

                runtime.packet_sequence += 1
                packet = {
                    "type": "audio_data",
                    "session_id": runtime.session_id,
                    "token": runtime.token,
                    "timestamp": time.time(),
                    "capture_timestamp": capture_timestamp,
                    "packet_sequence": runtime.packet_sequence,
                    "samples": samples_list,
                    "agent_instance_id": AGENT_INSTANCE_ID
                }

                if runtime.websocket:
                    await runtime.websocket.send(orjson.dumps(packet).decode('utf-8'))
                    runtime.send_fps_counter += 1
                    last_sent_version = current_version
                else:
                    log.warning("[runtime=%s] WS not open, sender exiting", local_gen)
                    return
            else:
                # Wait until the capture thread signals a new frame.
                # This replaces asyncio.sleep(0.005) — zero polling lag!
                runtime.new_frame_event.clear()
                try:
                    await asyncio.wait_for(runtime.new_frame_event.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Heartbeat timeout — loop back to check stop_event
    except asyncio.CancelledError:
        log.info("[runtime=%s] Sender loop cancelled", local_gen)
    except Exception as e:
        log.error("[runtime=%s] Sender loop error: %s", local_gen, e)


async def start_stream():
    global current_runtime, agent_state

    async with lifecycle_lock:
        if agent_state in [AgentState.STARTING, AgentState.STREAMING]:
            log.info("start_stream skipped: already %s", agent_state.value)
            return
        if agent_state == AgentState.STOPPING:
            log.warning("start_stream skipped: currently STOPPING")
            return

        log.info("[STATE] %s -> STARTING", agent_state.value)
        agent_state = AgentState.STARTING

        # Hard validation
        if current_runtime is not None:
            log.warning("start_stream found existing runtime. Forcing nullification.")
            current_runtime = None

        if not WS_URL:
            log.error("No WS_URL available. Cannot stream.")
            log.info("[STATE] STARTING -> IDLE")
            agent_state = AgentState.IDLE
            return

        runtime = StreamRuntime()
        local_gen = runtime.stream_instance_id
        log.info("[runtime=%s] Initializing new stream runtime", local_gen)
        
        # Audio Initialization
        runtime.audio_interface = pyaudio.PyAudio()
        device_index = find_loopback_device(runtime.audio_interface)
        
        try:
            runtime.audio_stream = runtime.audio_interface.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=PACKET_SIZE,
            )
        except Exception as e:
            log.error("[runtime=%s] Failed to open PyAudio stream: %s", local_gen, e)
            runtime.audio_interface.terminate()
            log.info("[STATE] STARTING -> IDLE")
            agent_state = AgentState.IDLE
            return

        # WS Connection
        ssl_context = None
        if WS_URL.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        try:
            runtime.websocket = await websockets.connect(WS_URL, ping_interval=None, ssl=ssl_context)
            log.info("[runtime=%s] WebSocket connected: %s", local_gen, WS_URL)

            try:
                sock = runtime.websocket.transport.get_extra_info("socket")
                if sock:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception as e:
                log.warning("[runtime=%s] Could not disable TCP_NODELAY: %s", local_gen, e)

            # Handshake
            await runtime.websocket.send(orjson.dumps({
                "type": "hello",
                "agent_version": VERSION,
                "platform": "windows",
                "mode": "desktop",
                "agent_instance_id": AGENT_INSTANCE_ID,
                "stream_instance_id": local_gen
            }).decode('utf-8'))

            raw_msg = await runtime.websocket.recv()
            init_data = orjson.loads(raw_msg)
            
            runtime.session_id = init_data.get("session_id")
            runtime.token = init_data.get("token")
            log.info("[runtime=%s] Session assigned: %s", local_gen, runtime.session_id)

        except websockets.exceptions.ConnectionClosed as exc:
            if exc.code in (1008, 4009):
                log.error("[runtime=%s] Connection rejected (Code %d). Duplicate/invalid.", local_gen, exc.code)
            else:
                log.error("[runtime=%s] WS connect error: %s", local_gen, exc)
            runtime.audio_stream.close()
            runtime.audio_interface.terminate()
            log.info("[STATE] STARTING -> IDLE")
            agent_state = AgentState.IDLE
            return
        except Exception as e:
            log.error("[runtime=%s] Start stream error: %s", local_gen, e)
            runtime.audio_stream.close()
            runtime.audio_interface.terminate()
            log.info("[STATE] STARTING -> IDLE")
            agent_state = AgentState.IDLE
            return

        current_runtime = runtime
        
        # Start Tasks
        runtime.recv_task = asyncio.create_task(ws_recv_loop(runtime))
        runtime.heartbeat_task = asyncio.create_task(heartbeat_loop(runtime))
        runtime.sender_task = asyncio.create_task(sender_loop(runtime))
        
        runtime.capture_thread = threading.Thread(target=capture_loop, args=(runtime, device_index), daemon=True)
        runtime.capture_thread.start()

        log.info("[STATE] STARTING -> STREAMING")
        agent_state = AgentState.STREAMING


async def stop_stream():
    global current_runtime, agent_state

    async with lifecycle_lock:
        if agent_state == AgentState.IDLE:
            return

        log.info("[STATE] %s -> STOPPING", agent_state.value)
        agent_state = AgentState.STOPPING

        if current_runtime is None:
            log.info("[STATE] STOPPING -> IDLE")
            agent_state = AgentState.IDLE
            return
            
        runtime = current_runtime
        local_gen = runtime.stream_instance_id
        log.info("[runtime=%s] Initiating clean teardown", local_gen)
        
        # Invalidate generation instantly for all loops
        current_runtime = None
        
        # Signal stop to loops
        runtime.stop_event.set()
        
        # Cancel all tasks
        tasks = []
        if runtime.sender_task:
            runtime.sender_task.cancel()
            tasks.append(runtime.sender_task)
        if runtime.heartbeat_task:
            runtime.heartbeat_task.cancel()
            tasks.append(runtime.heartbeat_task)
        if runtime.recv_task:
            runtime.recv_task.cancel()
            tasks.append(runtime.recv_task)
            
        # Join capture thread with timeout protection
        if runtime.capture_thread:
            try:
                await asyncio.wait_for(asyncio.to_thread(runtime.capture_thread.join, 5.0), timeout=6.0)
                if runtime.capture_thread.is_alive():
                    log.warning("[runtime=%s] Capture thread failed to join cleanly", local_gen)
            except asyncio.TimeoutError:
                log.warning("[runtime=%s] Capture thread join timed out", local_gen)

        # Close WS with timeout BEFORE gather
        if runtime.websocket:
            try:
                await asyncio.wait_for(runtime.websocket.close(), timeout=5.0)
            except Exception as e:
                log.warning("[runtime=%s] WebSocket close error/timeout: %s", local_gen, e)
                
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
                
        # Close PyAudio gracefully
        if runtime.audio_stream:
            try:
                runtime.audio_stream.stop_stream()
                runtime.audio_stream.close()
            except Exception as e:
                log.warning("[runtime=%s] Audio stream close error: %s", local_gen, e)
                
        if runtime.audio_interface:
            try:
                runtime.audio_interface.terminate()
            except Exception as e:
                log.warning("[runtime=%s] Audio interface terminate error: %s", local_gen, e)

        # Null out all references
        runtime.websocket = None
        runtime.audio_stream = None
        runtime.audio_interface = None
        runtime.capture_thread = None
        runtime.sender_task = None
        runtime.heartbeat_task = None
        runtime.recv_task = None
        runtime.latest_frame = None
        
        log.info("[STATE] STOPPING -> IDLE")
        agent_state = AgentState.IDLE


async def restart_stream():
    await stop_stream()
    await asyncio.sleep(1.0)
    await start_stream()


# ══════════════════════════════════════════════════════════════════════════════
# Main loop and Supervisor
# ══════════════════════════════════════════════════════════════════════════════

async def supervisor_loop():
    backoff = 2.0
    while not _shutdown.is_set():
        if desired_streaming:
            if agent_state == AgentState.IDLE:
                log.info("Supervisor detected desired_streaming with IDLE state. Starting...")
                await start_stream()
                backoff = 2.0
            elif agent_state == AgentState.STREAMING:
                if current_runtime and current_runtime.websocket and current_runtime.websocket.closed:
                    log.warning("Supervisor detected closed WebSocket while STREAMING. Restarting...")
                    await restart_stream()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    backoff = 2.0
        else:
            if agent_state != AgentState.IDLE:
                log.info("Supervisor detected not desired_streaming but state is %s. Stopping...", agent_state.value)
                await stop_stream()

        await asyncio.sleep(1.0)


async def main():
    global WS_URL, desired_streaming
    
    log.info("AudioCaptureAgent v%s starting.", VERSION)

    ws_override, launch_mode = parse_protocol_args()
    config = load_config()

    if ws_override:
        WS_URL = ws_override
        log.info("WS URL from protocol args: %s", WS_URL)
        config["last_server"] = WS_URL
        save_config(config)
    elif "last_server" in config:
        WS_URL = config["last_server"]
        log.info("WS URL from config: %s", WS_URL)

    if launch_mode == "stream":
        desired_streaming = True
        log.info("Stream mode activated via protocol args.")

    if WIN32_AVAILABLE:
        mutex = win32event.CreateMutex(None, False, "Global\\AudioAnalyzerAgentSingleton")
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            log.info("Another instance is already running.")
            if launch_mode == "stream":
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://127.0.0.1:{HEALTH_PORT}/start", data=b"", timeout=3)
                    log.info("Forwarded 'start' command to running instance.")
                except Exception as ipc_err:
                    log.error("IPC forward failed: %s", ipc_err)
            elif launch_mode == "stop":
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://127.0.0.1:{HEALTH_PORT}/stop", data=b"", timeout=3)
                    log.info("Forwarded 'stop' command to running instance.")
                except Exception as ipc_err:
                    log.error("IPC forward failed: %s", ipc_err)
            sys.exit(0)

    server_runner = await _start_health_server()
    
    supervisor_task = asyncio.create_task(supervisor_loop())

    try:
        await _shutdown.wait()
    finally:
        log.info("Initiating global shutdown.")
        desired_streaming = False
        await stop_stream()
        if supervisor_task:
            supervisor_task.cancel()
        if server_runner:
            await server_runner.cleanup()
        log.info("AudioCaptureAgent shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
