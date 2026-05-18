# Audio Waveform FFT Analyzer Dashboard

A comprehensive full-stack embedded and web system designed to capture analog audio, process the signal for real-time time-domain and frequency-domain analytics, and stream the data over WebSockets to a responsive web dashboard.

This project demonstrates a robust, scalable architecture for multi-device real-time signal analysis. It integrates high-performance embedded audio capture, an asynchronous event-driven backend, advanced digital signal processing (DSP), and a modern React-based visualization dashboard.

## 🌟 Key Features

### 📡 Multi-Session Acoustic Telemetry
- **Concurrent Device Streaming**: Supports multiple ESP32 or simulated devices streaming audio data simultaneously via multiplexed WebSockets.
- **Redis Stream Architecture**: Implements a robust data pipeline utilizing Redis consumer groups for O(1) session discovery and reliable, non-blocking asynchronous data ingestion.
- **Asynchronous Worker Pool**: Dedicated background workers dynamically spawn partitioned tasks (up to 64 concurrent sessions) to process streams without bottlenecking the FastAPI server.

### 🧮 Advanced Metrics Engine (DSP)
The custom DSP engine (`MetricsEngine`) performs continuous processing on 1024-sample packets:
- **Time-Domain Analysis**: RMS Energy, Peak Amplitude, Zero-Crossing Rate (ZCR).
- **Frequency-Domain Analysis (FFT)**: Peak Frequency estimation, Spectral Centroid, Spectral Rolloff (85%), and Spectral Flatness.
- **Rhythm Detection**: Autocorrelation-based BPM calculation utilizing an energy envelope over time.

### 💾 High-Performance Storage Layer
- **PostgreSQL Database**: Persistent storage for downsampled time-series audio metrics and session lifecycle management, accessed asynchronously via `asyncpg`.
- **Parquet Raw Storage**: Efficient columnar storage implementation for high-volume raw audio samples, optimizing disk I/O and enabling deep historical analysis.
- **Data Export**: Dedicated API endpoints for exporting session metrics and averages in JSON and CSV formats.

### 🖥️ Professional Analytic/intelligence Metrics from Raw Audio Samples
- **React + Vite Frontend**: High-performance, responsive UI crafted with a dark navy aesthetic.
- **Live Visualizations**: Synchronized scrolling oscilloscope (waveform),frequency spectrum (FFT) and zcr/rms/amp charts.
- **Session Management**: Live dashboard allows users to seamlessly switch subscriptions between active telemetry sessions.
<img width="1833" height="825" alt="Screenshot 2026-05-18 115255" src="https://github.com/user-attachments/assets/07e05d49-ac04-474f-ac39-c776d9de76a2" />
<img width="1840" height="628" alt="Screenshot 2026-05-18 115328" src="https://github.com/user-attachments/assets/6e67c2af-e7fd-461d-95d8-830e2803896f" />
<img width="1828" height="586" alt="Screenshot 2026-05-18 114701" src="https://github.com/user-attachments/assets/6b4c323e-1192-4ab5-abd9-2500be9c4403" />



---

## 🏗️ System Architecture

The architecture is entirely event-driven, decoupling ingestion from processing and presentation.

```mermaid
graph TD
    %% Hardware / Ingestion
    ESP[ESP32 MicroPython] -->|WebSocket JSON| API[FastAPI Ingest Endpoint]
    Mock[Demo Audio Script] -->|HTTP/WS| API
    
    %% Redis Stream Pipeline
    API -->|Raw Audio Payload| Redis[Redis Streams]
    
    %% Processing Worker
    Redis -->|Consumer Group XREAD| Worker[Asynchronous Worker Pool]
    Worker -->|FFT & Time-Domain| DSP[Metrics Engine]
    DSP -->|Structured Metrics| DB[(PostgreSQL)]
    DSP -->|Raw Samples| Parquet[(Parquet Storage)]
    DSP -->|Broadcaster Stream| RedisMetrics[Redis Metrics Stream]
    
    %% Dashboard
    RedisMetrics -->|Pub/Sub XREAD| API_WS[FastAPI Broadcaster]
    API_WS -->|WebSocket| Dash[React Dashboard]
    DB -->|Historical REST API| Dash
```

### Flow Overview
1. **Ingestion**: Audio devices send chunked sample arrays and tokens to the FastAPI publisher endpoint.
2. **Buffering**: Payloads are pushed to a Redis Stream partitioned by `session_id`.
3. **Processing**: The `StreamWorker` processes incoming batches, calculating FFT-based metrics and rhythmic patterns.
4. **Storage**: Analyzed metrics are batch-inserted into PostgreSQL while raw samples are flushed to Parquet files.
5. **Broadcasting**: A global async broadcaster reads processed metrics from Redis and fans them out to connected dashboard WebSockets.

---

## 📊 Session Computation & Analysis Assumptions

The `MetricsEngine` executes an array of computations locally, abiding by the following assumptions and constraints for accuracy and performance:

### Baseline Normalizations
- **ADC Scaling**: Assumes hardware feeds 12-bit audio samples centered at `2048`. The engine applies an offset and scales values to a normalized `[-1.0, 1.0]` float range.
- **Sampling Parameters**: Default operations assume a uniform `48,000 Hz` sampling rate. Processing runs in discrete chunks (packets) of `1024` samples.
- **Windowing**: A standard Hann Window is applied to the time-domain data prior to FFT conversion to minimize spectral leakage at the chunk boundaries.

### Analysis Thresholds & Constraints
- **Silence Gating**: An RMS threshold of `0.005` is utilized. If a packet's RMS energy falls beneath this value, the packet is flagged as silent, and all respective metrics (frequency, BPM, centroids) are zeroed out to prevent noise amplification.
- **Frequency Bounds**: For peak frequency analysis, spectral bins are constrained between `20 Hz` and `5000 Hz` to reject DC bias (0 Hz) and extreme high-frequency hardware noise.
- **Spectral Rolloff**: Computed dynamically targeting **85%** of the total signal energy distribution.

### Rhythm (BPM) Autocorrelation
- **Energy Envelope**: Computed sequentially across rolling buffers. A minimum history of 60 packets is required before BPM calculation initiates.
- **Beat Constraints**: Valid peak intervals are filtered to bounds between `0.3s` and `1.5s` to strictly yield physiological or musical tempos lying between `40` and `200` BPM.

### Session Aggregations
- **Post-Session Summaries**: When exporting or querying a finalized session via the REST API, the system computes arithmetic averages spanning all collected rows for RMS energy, Peak Amplitude, and BPM to characterize the holistic session profile.
- 

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

Access the Web Dashboard at: `http://localhost:3000`

### 🔧 Hardware Setup (ESP32)
1. **Analog Circuitry**:
   - Stereo AUX input mixed via 2x 4.7kΩ resistors.
   - AC coupling (1µF) and biased to 1.65V (2x 10kΩ divider).
   - Anti-alias filter (3.3kΩ + 10nF) fed into `GPIO34 (ADC1_CH6)`.
2. **Firmware**:
   - Flash MicroPython to the ESP32.
   - Upload the contents of the `micropython/` directory.
   - Update `wifi.py` and `streamer.py` with your network and backend IP credentials.

### 🧪 Running the Demo Source
If you do not have hardware, you can simulate a live session:
```bash
# Requires Python 3.8+
pip install -r backend/requirements.txt
python demo_audio_listener.py
```
