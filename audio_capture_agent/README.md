# AudioCaptureAgent

A production-ready Windows desktop helper that captures system audio (WASAPI loopback / Stereo Mix) and streams it directly into the Audio Waveform Analyzer backend — with no terminal, no Python installation, and no manual configuration required.

---

## Architecture

```
Browser Dashboard
     ↓
Checks http://127.0.0.1:47291/status (helper health)
     ↓
Launches helper via  audioanalyzer://start  (custom protocol)
     ↓
AudioCaptureAgent.exe  starts silently in the background
     ↓
Captures PC system audio (WASAPI loopback / Stereo Mix)
     ↓
Streams PCM packets  →  ws://localhost:8000/ws/stream
     ↓
Existing Redis → Worker → Dashboard pipeline (untouched)
```

---

## Files

| File | Purpose |
|---|---|
| `agent.py` | Main agent source |
| `requirements.txt` | Python dependencies |
| `build.bat` | One-click PyInstaller build |
| `register_protocol.reg` | Windows registry — `audioanalyzer://` protocol |
| `README.md` | This file |
| `icon.ico` | *(optional)* Application icon |

---

## First-Time Build

### Prerequisites

- Python 3.10+ (build machine only — end users do **not** need Python)
- A working audio input device (Stereo Mix, WASAPI loopback, or any input)

### Steps

```cmd
cd audio_capture_agent
build.bat
```

This will:
1. Install all Python dependencies via `pip`
2. Run PyInstaller with `--onefile --noconsole`
3. Output `dist\AudioCaptureAgent.exe`

### Manual build (alternative)

```cmd
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name AudioCaptureAgent --icon icon.ico agent.py
```

---

## Installation on End-User Machine

### Step 1 — Copy the executable

Copy `dist\AudioCaptureAgent.exe` into:

```
C:\Program Files\AudioCaptureAgent\AudioCaptureAgent.exe
```

> **Note:** You can use any directory, but you must update `register_protocol.reg` to match.

### Step 2 — Register the custom protocol

Double-click `register_protocol.reg` and click **Yes** when prompted.

This registers the `audioanalyzer://` URL scheme under `HKEY_CURRENT_USER` — **no administrator privileges required**.

#### Manual registration (alternative)

```cmd
reg import register_protocol.reg
```

#### To uninstall

```cmd
reg delete "HKCU\Software\Classes\audioanalyzer" /f
```

---

## Windows SmartScreen Warning

Because the executable is unsigned, Windows will show a SmartScreen warning on first launch:

> **Windows protected your PC**

**To proceed:**

1. Click **"More info"**
2. Click **"Run anyway"**

This is a one-time warning. After the first run, SmartScreen will not appear again for this file.

---

## Configuration

All settings are controlled via environment variables — no config file needed:

| Variable | Default | Description |
|---|---|---|
| `AUDIO_ANALYZER_WS` | `ws://localhost:8000/ws/stream` | Backend WebSocket URL |
| `AUDIO_ANALYZER_RATE` | `48000` | Sample rate (Hz) |
| `AUDIO_ANALYZER_CHANNELS` | `2` | Audio channels |
| `AUDIO_ANALYZER_PACKET` | `128` | Packet size (samples) |
| `AUDIO_ANALYZER_HEALTH_PORT` | `47291` | Health server port |

### Production deployment

For a cloud-hosted backend:

```cmd
set AUDIO_ANALYZER_WS=wss://yourdomain.com/ws/stream
AudioCaptureAgent.exe
```

---

## Health Endpoint

The agent runs a local HTTP server on startup:

```
GET http://127.0.0.1:47291/status
```

Response:
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

The browser dashboard uses this to detect whether the helper is installed before attempting to launch it via the protocol handler.

---

## Log File

Startup events and errors are written to:

```
%USERPROFILE%\AudioCaptureAgent.log
```

No console window is shown.

---

## Device Auto-Detection

The agent automatically searches for a loopback device in priority order:

1. `Stereo Mix` (Realtek / standard Windows)
2. `What U Hear` (Creative)
3. `Loopback` (generic)
4. `Wave Out Mix`
5. `VB-Audio Virtual Cable`
6. `BlackHole` (macOS — for future cross-platform support)
7. **Fallback:** Default system input device

---

## Packet Schema

Identical to the existing `demo_audio_listener.py` — the backend pipeline is untouched:

```json
{
  "type": "audio_data",
  "session_id": "uuid-assigned-by-server",
  "timestamp": 1716192000.123,
  "sample_rate": 48000,
  "waveform": "live_audio",
  "state": "SIGNAL",
  "samples": [2048, 2051, ...]
}
```

Samples are normalised to the 12-bit ESP32-compatible range `[0, 4095]`.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| No audio captured | Enable "Stereo Mix" in Windows Sound → Recording devices (right-click → Show Disabled Devices) |
| Health endpoint not reachable | Check Windows Firewall — allow `AudioCaptureAgent.exe` on loopback |
| Protocol handler not launching | Re-import `register_protocol.reg`, verify path matches exe location |
| SmartScreen blocks permanently | Right-click exe → Properties → Unblock |
| High CPU | Reduce `AUDIO_ANALYZER_PACKET` or increase `AUDIO_ANALYZER_RATE` |
| Log file location | `%USERPROFILE%\AudioCaptureAgent.log` |

---

## Performance Targets

| Metric | Target |
|---|---|
| CPU usage | < 5% (continuous streaming) |
| RAM usage | < 100 MB steady-state |
| WebSocket reconnect | Exponential backoff, 2s → 60s cap |
| Heartbeat | Ping every 30 seconds |
| Packet pacing | `asyncio.sleep(PACKET_SIZE / SAMPLE_RATE)` |
