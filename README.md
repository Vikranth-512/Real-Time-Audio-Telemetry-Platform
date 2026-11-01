# Real-Time Audio Telemetry Platform

A comprehensive full-stack embedded and web system designed to capture analog audio, process the signal for real-time time-domain and frequency-domain analytics

## 🏗️ System Architecture

The architecture is entirely event-driven, decoupling ingestion from processing and presentation.

```mermaid
graph TD
    %% Audio Capture Sources
    ESP[ESP32 MicroPython Device] -->|WebSocket JSON| API[FastAPI Ingestion Layer]
    
    Native[Desktop Audio Capture Agent] -->|WSS Audio Stream| Caddy[Caddy Reverse Proxy]
    Caddy -->|/ws/stream| API

    %% Reverse Proxy / Frontend
    User[React Dashboard Client] -->|HTTPS / WSS| Caddy
    Caddy -->|Static Frontend| Frontend[React + Vite Frontend]

    %% Redis Stream Pipeline
    API -->|Raw Audio Payload| Redis[Redis Streams]

    %% Processing Worker Pipeline
    Redis -->|Consumer Group XREAD partitioned by Session ID| Worker[Asynchronous Worker Pool]
    Worker -->|Partitioned Stream Tasks| WorkerTask[Session Worker Task]

    %% DSP / Analytics
    WorkerTask -->|FFT + Time Domain Analysis| DSP[Metrics & Inference Engine]

    %% Storage
    DSP -->|Structured Metrics| DB[(PostgreSQL)]
    DSP -->|Raw Samples + Session Data| Parquet[(Parquet Storage)]

    %% Real-Time Metrics Broadcast
    DSP -->|Processed Metrics Stream| RedisMetrics[Redis Metrics Stream]
    RedisMetrics -->|Pub/Sub XREAD| API_WS[FastAPI WebSocket Broadcaster]

    %% Frontend Live Updates
    API_WS -->|Realtime WebSocket Metrics| Dash[React Dashboard]

    %% Historical Queries / Exports
    DB -->|Historical Session Queries| Dash
    DB -->|Session Export API| ExportAPI[Export API Endpoint]
    ExportAPI -->|JSON / CSV Download| Dash
```

## 🌟 Key Features

###  Professional Analytic/intelligence Metrics from Raw Audio Samples
- **React + Vite Frontend**: High-performance, responsive UI crafted with a dark navy aesthetic.
- **Live Visualizations**: multiple live audio analylitic views including Synchronized scrolling oscilloscope (waveform),frequency spectrum (FFT) and zcr/rms/amp charts.
- **Session Management**: Live dashboard allows users to seamlessly switch subscriptions between active telemetry sessions.

<img width="1878" height="906" alt="Screenshot (443)" src="https://github.com/user-attachments/assets/c01a97de-e792-42a0-a538-c1281f34453b" />
<img width="1852" height="686" alt="Screenshot (437)" src="https://github.com/user-attachments/assets/c2bf1e80-b4c7-49ae-ab6e-c00975e8d76a" />

## Performance Optimizations

This architecture has been heavily optimized for low-latency, real-time audio telemetry, ensuring a buttery-smooth visual experience without compromising backend scalability.

### Real-Time Performance

| Metric | Value | Details |
| :--- | :---: | :--- |
| **Render Frame Rate** | `60 / 120 FPS` | Vsync-locked via `requestAnimationFrame` — no manual throttle |
| **Render Backend** | `WebGL` | Hardware-accelerated `LINE_STRIP` draw, zero Canvas2D fallback |
| **Heap Allocations per Frame** | `0` | Pre-allocated `Float32Array(12,000)` vertex buffer, reused every frame |
| **Capture → Send Latency** | `< 1 ms` | Event-driven dispatch (`asyncio.Event`), zero polling delay |
| **Audio Capture Rate** | `48,000 Hz` | 1024-sample packets → ~47 packets/sec |
| **Ring Buffer Depth** | `48,000 samples` | 1 full second of audio history in memory |
| **Display Window** | `6,000 samples` | ~125 ms of waveform visible at any time |
| **Visual Smoothing** | `Spring Physics` | Lerp factor `0.08` decouples 47 FPS data from 60 FPS render |
| **Stale Packet Rejection** | `< 500 ms` | Packets exceeding 500 ms end-to-end latency are silently dropped |
| **Sequence Guard** | `Monotonic` | Out-of-order and replayed packets rejected via `packet_sequence` counter |
| **React Re-render Throttle** | `100 ms` | Metric state updates batched — only 10 React re-renders/sec max |
| **Backend JSON Overhead** | `Zero-copy` | Sample arrays pass through Redis as raw strings, never re-serialized |
| **FFT Compute** | `1× per packet` | Single 1024-point FFT shared across all spectral metric calculations |
| **WebSocket Heartbeat** | `30 s` | Application-level ping keeps connection alive through reverse proxies |
| **Redis Stream Depth** | `50 msgs` | Bounded `MAXLEN` per session prevents unbounded memory growth |

## Session Intelligence Engine

The Session Intelligence Engine is a high-level acoustic analytics layer built on top of the real-time audio processing pipeline.

It transforms low-level waveform features into interpretable behavioral and spectral insights using lightweight statistical heuristics and rolling-window signal analysis.

The system continuously evaluates:

- RMS energy
- Peak amplitude
- Dominant frequency
- Zero Crossing Rate (ZCR)
- Temporal burst density
- Frequency drift
- Spectral consistency
- Activity transitions

This enables real-time classification of acoustic behavior, environmental instability, noise conditions, spike events, and temporal signal dynamics.

---

<img width="1833" height="825" alt="Screenshot 2026-05-18 115255" src="https://github.com/user-attachments/assets/07e05d49-ac04-474f-ac39-c776d9de76a2" />
<img width="1840" height="628" alt="Screenshot 2026-05-18 115328" src="https://github.com/user-attachments/assets/6e67c2af-e7fd-461d-95d8-830e2803896f" />
<img width="1828" height="586" alt="Screenshot 2026-05-18 114701" src="https://github.com/user-attachments/assets/6b4c323e-1192-4ab5-abd9-2500be9c4403" />


### Core Intelligence Modules


---

```mermaid
flowchart LR
    A[Audio Stream] --> B[Frame Windowing]
    B --> C[Feature Extraction]

    C --> D1[RMS Energy]
    C --> D2[Amplitude]
    C --> D3[Dominant Frequency]
    C --> D4[Zero Crossing Rate]

    D1 --> E[Intelligence Layer]
    D2 --> E
    D3 --> E
    D4 --> E

    E --> F1[Pattern Classification]
    E --> F2[Drift Detection]
    E --> F3[Spike Detection]
    E --> F4[Timeline Segmentation]

    F1 --> G[Session Intelligence Report]
    F2 --> G
    F3 --> G
    F4 --> G

    classDef source fill:#0f172a,stroke:#38bdf8,color:#e2e8f0,stroke-width:2px;
    classDef process fill:#111827,stroke:#818cf8,color:#f8fafc,stroke-width:2px;
    classDef feature fill:#1e293b,stroke:#22c55e,color:#f8fafc,stroke-width:2px;
    classDef intelligence fill:#312e81,stroke:#facc15,color:#f8fafc,stroke-width:3px;
    classDef output fill:#3f1d2e,stroke:#fb7185,color:#f8fafc,stroke-width:3px;

    class A source;
    class B,C process;
    class D1,D2,D3,D4 feature;
    class E,F1,F2,F3,F4 intelligence;
    class G output;
```

### Session Timeline Segmentation

The session timeline converts continuous audio into classified behavioral regions using rolling-window feature analysis.

Each segment is dynamically categorized based on spectral activity and energy distribution.

---

### Timeline States

| State | Meaning |
|---|---|
| `Quiet` | Minimal signal activity |
| `Stable` | Consistent harmonic behavior |
| `Active` | Elevated acoustic activity |
| `Burst-Heavy` | Frequent transient spikes |
| `Chaotic` | Highly unstable acoustic behavior |

---

## Timeline Analysis Engine

The Timeline Analysis Engine transforms raw acoustic metrics into a structured, observability-grade interpretation of session behavior. Rather than simply plotting RMS values over time, the engine performs multi-stage signal analysis to identify activity patterns, environmental shifts, anomalies, behavioral phases, and long-term trends.

<img width="1852" height="835" alt="image" src="https://github.com/user-attachments/assets/ea75c5af-1cc2-467c-8c93-2e75bccd3b32" />
<img width="1816" height="674" alt="Screenshot (470)" src="https://github.com/user-attachments/assets/8a93d68e-e5da-4288-b8bc-6c4b7862ed87" />

---

### Multi-Timescale Baseline Modeling

The analyzer maintains independent baseline models to separate short-term activity from long-term environmental conditions.

#### Fast Baseline
- Detects local activity changes
- Powers Active/Burst classification
- Preserves responsiveness to transient events

#### Slow Baseline
- Models the underlying acoustic environment
- Used for environmental shift detection
- Prevents sustained activity from being absorbed into the baseline

This dual-timescale architecture allows the system to distinguish between different acoustic behaviors:

| Scenario | Classification |
|-----------|---------------|
| Short loud spike | Burst |
| Sustained conversation | Active |
| Permanent environmental change | Environment Shift |
| Stable ambient sound | Steady |

---

### Structural Phase Segmentation

The analyzer converts low-level state transitions into human-readable behavioral phases.

Example phase sequence:

```text
Steady State
    ↓
Sustained Activity
    ↓
Burst Cluster
    ↓
Steady State
```

Each phase contains:

- Start time
- End time
- Duration
- Confidence score

---

## Validation & Reliability

Validation focuses on:

- False-positive suppression
- False-negative reduction
- Baseline stability
- Event accuracy
- Environment shift reliability
- Timeline confidence correctness

---

## Output Schema

The analyzer produces a structured session interpretation:

```json
{
  "baseline": {},
  "distribution": {},
  "segments": [],
  "events": [],
  "anomalies": [],
  "phases": [],
  "insights": {},
  "summary": ""
}
```

### Output Components

| Component | Description |
|------------|------------|
| baseline | Session-wide acoustic baseline metrics |
| distribution | Percentage distribution of activity states |
| segments | Classified timeline segments |
| events | Extracted acoustic events |
| anomalies | Clustered anomaly detections |
| phases | Human-readable behavioral phases |
| insights | Derived observability metrics |
| summary | Fact-based narrative description |

---

## Design Goals

The Timeline Analysis Engine is designed around the following principles:

- Deterministic results
- Explainable classifications
- Low false-positive rates
- Robust handling of missing data
- Real-time compatibility
- Observability-grade interpretability
- Frontend-stable API contract
- Human-readable outputs
- Statistically robust analysis

---

### Role in the Platform

The Timeline Analysis Engine serves as the intelligence layer that converts raw acoustic measurements into meaningful operational insights.

It enables users to understand not only **what happened**, but also:

- How the acoustic environment evolved
- When meaningful events occurred
- Whether environmental conditions changed
- How stable or dynamic a session was
- Which anomalies were structurally significant

By combining signal processing, statistical modeling, anomaly detection, and semantic interpretation, the engine transforms raw audio metrics into actionable observability insights.

---

## Technical Characteristics

| Capability | Description |
|---|---|
| **Real-Time Processing** | Continuous streaming acoustic analysis |
| **Lightweight Heuristics** | No heavyweight ML inference required |
| **Stream-Oriented Architecture** | Compatible with Redis stream pipelines |
| **Temporal Analysis** | Rolling-window behavioral segmentation |
| **Spectral Intelligence** | Frequency-aware acoustic interpretation |
| **Live Visualization** | WebSocket-driven dashboard updates |
| **Session Summarization** | End-of-session intelligence synthesis |

---

### System Design Goals

- Low-latency streaming analysis
- Lightweight computational footprint
- Real-time dashboard responsiveness
- Interpretable acoustic intelligence
- Modular feature extraction pipeline
- Extensible behavioral classification system

### Flow Overview
1. **Ingestion**: Audio devices send chunked sample arrays and tokens to the FastAPI publisher endpoint.
2. **Buffering**: Payloads are pushed to a Redis Stream partitioned by `session_id`.
3. **Processing**: The `StreamWorker` (part of the asynchronous worker pool) processes incoming stream batches using consumer groups, calculating FFT-based metrics and rhythmic patterns.
4. **Storage**: Analyzed metrics are batch-inserted into PostgreSQL while raw samples are flushed to Parquet files.
5. **Broadcasting**: A global async broadcaster reads processed metrics from Redis and fans them out to connected dashboard WebSockets.
6. **Data Export**: The dashboard or external clients can request session data exports via REST API, retrieving historical session metrics and raw data in structured formats like JSON or CSV.

## 🚀 Getting Started

### Prerequisites
- Docker & Docker Compose
- Node.js 16+ & Python 3.8+ (for local development)

### 🐳 Docker Deployment (Recommended)
The fastest way to run the entire stack (PostgreSQL, Redis, FastAPI Backend, Processing Worker, Nginx Frontend) is via Docker Compose:

```bash
# Build and start all services
docker-compose up --build

# To stop the containers
docker-compose down
```

Access the Web Dashboard at: `http://localhost`

