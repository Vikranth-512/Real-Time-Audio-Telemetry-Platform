import React, { useState, useEffect, useCallback, useRef } from 'react'
import WaveformVisualization from './components/WaveformVisualization'
import MetricsPanel from './components/MetricsPanel'
import ConnectionStatus from './components/ConnectionStatus'
import AverageMetricsPanel from './components/AverageMetricsPanel'
import SessionSidebar, { HistoryIcon } from './components/SessionSidebar'
import LiveAcousticPanel from './components/LiveAcousticPanel'
import InferenceAnalyticsPanel from './components/InferenceAnalyticsPanel'
import TimelinePanel from './components/TimelinePanel'
import './styles.css'
import { useWebSocket } from './hooks/useWebSocket'

// ── Constants ─────────────────────────────────────────────────────────────────
const HELPER_PROTOCOL = 'audioanalyzer://start'
const HELPER_DOWNLOAD = '/downloads/AudioCaptureAgent.exe'


// ── Desktop Stream Modal ───────────────────────────────────────────────────────
function DesktopStreamModal({ onClose }) {
    const handleDownload = useCallback(() => {
        const a = document.createElement('a')
        a.href = HELPER_DOWNLOAD
        a.download = 'AudioCaptureAgent.exe'
        document.body.appendChild(a)
        a.click()
        a.remove()
    }, [])

    return (
        <div className="desktop-modal-backdrop" onClick={onClose}>
            <div className="desktop-modal" onClick={e => e.stopPropagation()}>
                <button className="desktop-modal__close" onClick={onClose} aria-label="Close">✕</button>

                <div className="desktop-modal__icon desktop-modal__icon--warn">⬇</div>
                <h3 className="desktop-modal__title">Desktop Helper Required</h3>
                <p className="desktop-modal__desc">
                    AudioCaptureAgent captures your PC's system audio and streams it here.
                    Install it once — future launches are automatic.
                </p>
                <div className="desktop-modal__steps">
                    <div className="desktop-modal__step">
                        <span className="desktop-modal__step-num">1</span>
                        <span>Download and run <strong>AudioCaptureAgent.exe</strong></span>
                    </div>
                    <div className="desktop-modal__step">
                        <span className="desktop-modal__step-num">2</span>
                        <span>Double-click <strong>register_protocol.reg</strong> (one-time)</span>
                    </div>
                    <div className="desktop-modal__step">
                        <span className="desktop-modal__step-num">3</span>
                        <span>Launch it manually or via <a href="#" onClick={(e) => { e.preventDefault(); window.location.href = HELPER_PROTOCOL; }}>audioanalyzer://start</a></span>
                    </div>
                </div>
                <button className="desktop-modal__btn" style={{ marginBottom: "8px" }} onClick={handleDownload}>
                    ⬇ Download AudioCaptureAgent.exe
                </button>
                <button className="desktop-modal__btn" onClick={() => { window.location.href = HELPER_PROTOCOL; }}>
                    ▶ Launch Helper
                </button>
                <p className="desktop-modal__hint">
                    Windows SmartScreen may warn — click <em>More info → Run anyway</em>.
                </p>
            </div>
        </div>
    )
}

// ─── Session list sidebar ─────────────────────────────────────────────────────
function SessionListPanel({ sessions, selectedId, onSelect }) {
    const entries = Object.values(sessions)
    if (entries.length === 0) return null

    return (
        <div className="session-list-panel">
            <div className="session-list-panel__title">Live Sessions</div>
            {entries.map(s => {
                const isSelected = s.sessionId === selectedId
                const isLive = s.status === 'live'
                const rate = s.packetRate ? `${s.packetRate.toFixed(1)} pkt/s` : ''
                const ago = s.lastSeen
                    ? `${Math.round((Date.now() / 1000) - s.lastSeen)}s ago`
                    : ''
                return (
                    <div
                        key={s.sessionId}
                        className={`session-list-item ${isSelected ? 'session-list-item--active' : ''}`}
                        onClick={() => onSelect(s.sessionId)}
                    >
                        <div className="session-list-item__row">
                            <span className={`session-list-item__dot ${isLive ? 'session-list-item__dot--live' : 'session-list-item__dot--ended'}`} />
                            <span className="session-list-item__id" title={s.sessionId}>
                                {s.sessionId.length > 8 ? `${s.sessionId.substring(0, 8)}...` : s.sessionId}
                            </span>
                        </div>
                        <div className="session-list-item__meta">
                            <span>{s.deviceId || 'device'}</span>
                            {rate && <span>{rate}</span>}
                            {ago && <span>{ago}</span>}
                        </div>
                    </div>
                )
            })}
        </div>
    )
}

// ─── App ─────────────────────────────────────────────────────────────────────
function App() {

    // ── Multi-session registry ─────────────────────────────────────────────
    // sessions: { [sessionId]: { sessionId, deviceId, status, lastSeen, packetRate, _pktCount, _pktWindow } }
    const [sessions, setSessions] = useState({})
    const [selectedSessionId, setSelectedSessionId] = useState(null)
    const selectedSessionIdRef = useRef(null)
    useEffect(() => { selectedSessionIdRef.current = selectedSessionId }, [selectedSessionId])

    // ── Per-selected-session UI state ──────────────────────────────────────
    const [currentMetrics, setCurrentMetrics] = useState({
        bpm: 0, rms: 0, peak: 0, frequency: 0,
        peak_frequency: 0, spectral_centroid: 0, spectral_rolloff: 0, spectral_flatness: 0,
    })
    const [sessionTime, setSessionTime] = useState(0)
    const sessionTimeRef = useRef(0)
    const waveformBufferRef = useRef(new Float32Array(48000))
    const waveformIndexRef = useRef(0)
    const lastPacketSequenceRef = useRef(-1)
    const [liveLatency, setLiveLatency] = useState(null)
    const liveLatencyRef = useRef(null)
    const [sessionStartTime, setSessionStartTime] = useState(null)
    const sessionStartTimeRef = useRef(null)
    const [sessionActive, setSessionActive] = useState(false)
    const sessionActiveRef = useRef(false)
    const [sessionStopped, setSessionStopped] = useState(false)
    const [averages, setAverages] = useState(null)
    const [timeline, setTimeline] = useState(null)
    const [currentSessionId, setCurrentSessionId] = useState(null)
    const [archivedSessionId, setArchivedSessionId] = useState(null)

    // ── Throttled metric update refs (avoid per-packet React re-renders) ───
    const metricsFlushTimerRef = useRef(null)
    const pendingMetricsRef = useRef(null)
    const pktWindowRef = useRef({ count: 0, windowStart: Date.now(), lastRate: 0 })
    const pktRateFlushTimerRef = useRef(null)

    // ── UI state ───────────────────────────────────────────────────────────
    const [sidebarOpen, setSidebarOpen] = useState(false)
    const [showFFT, setShowFFT] = useState(false)
    const [waveformResetKey, setWaveformResetKey] = useState(0)
    const [showDesktopModal, setShowDesktopModal] = useState(false)

    const timerRef = useRef(null)

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/audio`
    const { isConnected, connectionStatus, send, subscribe, subscribeToSession, subscribeRaw } =
        useWebSocket(wsUrl)


    // ── Global events (session discovery) ─────────────────────────────────
    useEffect(() => {
        const unsub = subscribe((data) => {

            if (data.type === 'active_sessions') {
                // Bootstrap: backend sends current live sessions on connect
                setSessions(prev => {
                    const next = { ...prev }
                    for (const s of (data.sessions || [])) {
                        if (!next[s.session_id]) {
                            next[s.session_id] = {
                                sessionId: s.session_id,
                                deviceId: s.device_id || 'device',
                                status: 'live',
                                lastSeen: null,
                                packetRate: 0,
                                _pktCount: 0,
                                _pktWindow: Date.now(),
                            }
                        }
                        // Subscribe to this session's data stream
                        send({ type: 'subscribe', session_id: s.session_id })
                    }
                    return next
                })
                return
            }

            if (data.type === 'session_discovered') {
                const sid = data.session_id
                setSessions(prev => {
                    if (prev[sid]) return prev
                    return {
                        ...prev,
                        [sid]: {
                            sessionId: sid,
                            deviceId: data.device_id || 'device',
                            status: 'live',
                            lastSeen: null,
                            packetRate: 0,
                            _pktCount: 0,
                            _pktWindow: Date.now(),
                        }
                    }
                })
                send({ type: 'subscribe', session_id: sid })
                return
            }

            if (data.type === 'session_ended') {
                const sid = data.session_id
                setSessions(prev => {
                    if (!prev[sid]) return prev
                    return { ...prev, [sid]: { ...prev[sid], status: 'ended' } }
                })
                // If the currently watched session ended, reflect that in UI
                if (selectedSessionIdRef.current === sid) {
                    setSessionActive(false)
                    setSessionStopped(true)
                    setArchivedSessionId(sid)

                    // CLEAR: zero out visualization buffers natively
                    waveformBufferRef.current.fill(0)
                    waveformIndexRef.current = 0
                    lastPacketSequenceRef.current = -1
                    setLiveLatency(null)
                    setCurrentMetrics({
                        bpm: 0, rms: 0, peak: 0, frequency: 0,
                        peak_frequency: 0, spectral_centroid: 0, spectral_rolloff: 0, spectral_flatness: 0,
                    })
                    setWaveformResetKey(k => k + 1)
                }
                return
            }
        })
        return unsub
    }, [subscribe, send])

    // Auto-select first live session that appears
    useEffect(() => {
        if (selectedSessionId) return
        const live = Object.values(sessions).find(s => s.status === 'live')
        if (live) {
            setSelectedSessionId(live.sessionId)
            sessionActiveRef.current = false  // will be set true on first packet
            sessionStartTimeRef.current = null
            setSessionActive(false)
            setSessionStopped(false)
            setArchivedSessionId(null)
            setAverages(null)
            setTimeline(null)
        }
    }, [sessions, selectedSessionId])

    // ── Track packet rate per session + update selectedSession UI state ────
    const lastPktTimes = useRef({}) // session_id -> circular ts array for rate calc

    useEffect(() => {
        if (!selectedSessionId) return

        const unsubSession = subscribeToSession(selectedSessionId, (data) => {
            if (data.type !== 'audio_update') return

            // ── Sequence guard (drop stale/replayed packets) ───────────────
            if (data.packet_sequence !== undefined) {
                if (data.packet_sequence <= lastPacketSequenceRef.current) return
                lastPacketSequenceRef.current = data.packet_sequence
            }

            // ── Write samples into ring buffer (zero React state calls) ───
            const samples = data.samples
            if (samples && samples.length > 0) {
                const buffer = waveformBufferRef.current
                let idx = waveformIndexRef.current
                for (let i = 0; i < samples.length; i++) {
                    buffer[idx] = samples[i]
                    idx = (idx + 1) % 48000
                }
                waveformIndexRef.current = idx
            }

            // ── Throttled metrics flush (max 1 React update per 100ms) ────
            const metrics = data.metrics || {}
            pendingMetricsRef.current = {
                bpm: metrics.bpm || 0,
                rms: metrics.rms || 0,
                peak: metrics.peak || 0,
                frequency: metrics.frequency || 0,
                peak_frequency: metrics.peak_frequency || 0,
                spectral_centroid: metrics.spectral_centroid || 0,
                spectral_rolloff: metrics.spectral_rolloff || 0,
                spectral_flatness: metrics.spectral_flatness || 0,
            }
            if (!metricsFlushTimerRef.current) {
                metricsFlushTimerRef.current = setTimeout(() => {
                    if (pendingMetricsRef.current) {
                        setCurrentMetrics(m => ({ ...m, ...pendingMetricsRef.current }))
                        pendingMetricsRef.current = null
                    }
                    metricsFlushTimerRef.current = null
                }, 100)
            }

            // ── Latency update (ref-first, flush every 500ms) ─────────────
            if (data.latency !== undefined) {
                liveLatencyRef.current = data.latency
            }

            // ── Session activation (only once per session) ────────────────
            if (!sessionActiveRef.current) {
                sessionActiveRef.current = true
                sessionStartTimeRef.current = Date.now()
                setSessionActive(true)
                setSessionStartTime(Date.now())
            }

            // ── Packet rate: accumulate in ref, flush to state every 2s ──
            const pw = pktWindowRef.current
            pw.count += 1
            const elapsed = (Date.now() - pw.windowStart) / 1000
            if (elapsed >= 2) {
                const rate = pw.count / elapsed
                pw.count = 0
                pw.windowStart = Date.now()
                pw.lastRate = rate
                if (!pktRateFlushTimerRef.current) {
                    pktRateFlushTimerRef.current = setTimeout(() => {
                        setSessions(prev => {
                            const s = prev[selectedSessionId]
                            if (!s) return prev
                            return { ...prev, [selectedSessionId]: { ...s, packetRate: pw.lastRate, lastSeen: Date.now() / 1000 } }
                        })
                        pktRateFlushTimerRef.current = null
                    }, 0)
                }
            }
        })

        return () => {
            unsubSession()
            // Clean up throttle timers on session change
            if (metricsFlushTimerRef.current) { clearTimeout(metricsFlushTimerRef.current); metricsFlushTimerRef.current = null }
            if (pktRateFlushTimerRef.current) { clearTimeout(pktRateFlushTimerRef.current); pktRateFlushTimerRef.current = null }
        }
    }, [selectedSessionId, subscribeToSession])

    // ── Dashboard glow ─────────────────────────────────────────────────────
    useEffect(() => {
        document.documentElement.style.setProperty(
            '--wave-glow', Math.min((currentMetrics.rms || 0) * 3, 1)
        )
    }, [currentMetrics.rms])

    // ── Session timer ──────────────────────────────────────────────────────
    useEffect(() => {
        if (!sessionActive || !sessionStartTime || sessionStopped) return
        timerRef.current = setInterval(() => {
            setSessionTime(Math.floor((Date.now() - sessionStartTime) / 1000))
        }, 1000)
        return () => { clearInterval(timerRef.current); timerRef.current = null }
    }, [sessionActive, sessionStartTime, sessionStopped])

    // ── Derived (needed by handlers below) ────────────────────────────────
    const activeSessionId = selectedSessionId && sessions[selectedSessionId]?.status === 'live' ? selectedSessionId : null

    // ── Handlers ───────────────────────────────────────────────────────────

    const handleStopSession = useCallback(() => {
        const sid = selectedSessionId
        if (!sid) return
        const mode = showFFT ? 'fft' : 'wave'

        fetch(`/api/sessions/${sid}/stop`, { method: 'POST' })
            .then(r => r.json())
            .then(() => {
                clearInterval(timerRef.current); timerRef.current = null
                setSessionStopped(true)
                setSessionActive(false)
                setArchivedSessionId(sid)
                setSelectedSessionId(null)
                setSessionStartTime(null)
                setLiveLatency(null)
                setSessions(prev => ({
                    ...prev,
                    [sid]: { ...prev[sid], status: 'ended' }
                }))

                // Fetch averages for the stopped session
                fetch(`/api/session/${sid}/metrics?mode=${mode}`)
                    .then(r => r.json())
                    .then(data => {
                        setAverages(data.averages || {})
                        setTimeline(data.timeline || null)
                        setCurrentSessionId(data.session_id)
                    })
                    .catch(err => console.error('Averages fetch failed', err))
            })
    }, [selectedSessionId, showFFT])

    const launchLockRef = useRef(false);

    const launchDesktopAgent = (mode = 'stream') => {
        const serverOrigin = window.location.origin;
        const protocolUrl = `audioanalyzer://start?server=${encodeURIComponent(serverOrigin)}&mode=${mode}`;

        const iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        iframe.src = protocolUrl;
        document.body.appendChild(iframe);

        setTimeout(() => {
            if (document.body.contains(iframe)) {
                document.body.removeChild(iframe);
            }
        }, 2000);
    };

    const handleDesktopAudioClick = useCallback(() => {
        // If a session is already active, stop it (agent disconnects → session ends naturally)
        if (activeSessionId) {
            handleStopSession();
            // Also signal the agent to stop streaming via protocol IPC
            launchDesktopAgent('stop');
            return;
        }

        if (launchLockRef.current) return;
        launchLockRef.current = true;
        launchDesktopAgent('stream');

        setTimeout(() => {
            launchLockRef.current = false;
        }, 3000);
    }, [activeSessionId, handleStopSession]);

    const handleRefreshWaveform = useCallback(() => {
        waveformBufferRef.current.fill(0)
        waveformIndexRef.current = 0
        lastPacketSequenceRef.current = -1
        setWaveformResetKey(prev => prev + 1)
    }, [])

    const handleSelectSession = useCallback((sid) => {
        if (sid === selectedSessionId) return
        // Save current session as archived if it was live
        if (selectedSessionId && sessions[selectedSessionId]?.status === 'live') {
            setArchivedSessionId(selectedSessionId)
        }
        setSelectedSessionId(sid)
        const s = sessions[sid]
        const isLive = s?.status === 'live'
        setSessionActive(isLive)
        setSessionStopped(!isLive)
        setArchivedSessionId(!isLive ? sid : null)
        setSessionStartTime(isLive ? Date.now() : null)
        setSessionTime(0)
        waveformBufferRef.current.fill(0)
        waveformIndexRef.current = 0
        lastPacketSequenceRef.current = -1
        setLiveLatency(null)
        setAverages(null)
        setTimeline(null)
        setCurrentSessionId(sid)
        setWaveformResetKey(prev => prev + 1)
    }, [selectedSessionId, sessions])

    const handleStartNewSession = useCallback(() => {
        setSessionStopped(false)
        setSessionActive(false)
        setSelectedSessionId(null)
        setArchivedSessionId(null)
        setCurrentSessionId(null)
        setAverages(null)
        setTimeline(null)
        waveformBufferRef.current.fill(0)
        waveformIndexRef.current = 0
        lastPacketSequenceRef.current = -1
        setLiveLatency(null)
        setSessionStartTime(null)
        setSessionTime(0)
        setWaveformResetKey(prev => prev + 1)
    }, [])



    const handleLoadSession = useCallback((sessionId) => {
        setArchivedSessionId(sessionId)
        setCurrentSessionId(sessionId)
        setSelectedSessionId(sessionId)
        setSessionStopped(true)
        setSessionActive(false)
        setSidebarOpen(false)

        const mode = showFFT ? 'fft' : 'wave'
        fetch(`/api/session/${sessionId}/metrics?mode=${mode}`)
            .then(r => r.json())
            .then(data => {
                setAverages(data.averages || {})
                setTimeline(data.timeline || null)
            })
            .catch(err => console.error('Metrics fetch failed', err))
    }, [showFFT])

    const handleExportAverages = useCallback(() => {
        const id = archivedSessionId || currentSessionId || selectedSessionId
        if (!id) return
        const mode = showFFT ? 'fft' : 'wave'
        fetch(`/api/session/${id}/averages?mode=${mode}`)
            .then(r => r.json())
            .then(data => {
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = `averages_${id}.json`
                document.body.appendChild(a)
                a.click()
                a.remove()
                URL.revokeObjectURL(url)
            })
            .catch(err => console.error('Export averages failed', err))
    }, [archivedSessionId, currentSessionId, selectedSessionId, showFFT])

    const handleExportSession = useCallback(() => {
        const id = archivedSessionId || currentSessionId || selectedSessionId
        if (!id) return
        const mode = showFFT ? 'fft' : 'wave'
        fetch(`/api/session/${id}/metrics?mode=${mode}`)
            .then(r => r.json())
            .then(data => {
                if (!data.full_metrics || data.full_metrics.length === 0) {
                    console.warn("No metrics to export"); return
                }

                const lines = ['timestamp,rms,amplitude,bpm,frequency,zcr,rolling_mean_rms,rolling_variance,rolling_peak_amplitude']
                const windowSize = 30
                let sumRms = 0, sumSqRms = 0
                const maxDeque = []

                // Bucket into 1-second intervals
                const secondBuckets = new Map()
                for (const row of data.full_metrics) {
                    const m = row.metrics
                    const bucket = Math.floor(row.timestamp)
                    if (!secondBuckets.has(bucket)) secondBuckets.set(bucket, { rms: 0, peak: 0, bpm: 0, freq: 0, zcr: 0, count: 0 })
                    const b = secondBuckets.get(bucket)
                    b.rms += m.rms || 0; b.peak += m.peak || 0; b.bpm += m.bpm || 0
                    b.freq += m.frequency || 0; b.zcr += m.zcr || 0; b.count += 1
                }

                const perSecond = [...secondBuckets.keys()].sort((a, b) => a - b).map(ts => {
                    const b = secondBuckets.get(ts)
                    return { ts, rms: b.rms / b.count, peak: b.peak / b.count, bpm: b.bpm / b.count, freq: b.freq / b.count, zcr: b.zcr / b.count }
                })

                for (let i = 0; i < perSecond.length; i++) {
                    const { ts, rms, peak, bpm, freq, zcr } = perSecond[i]
                    sumRms += rms; sumSqRms += rms * rms
                    while (maxDeque.length > 0 && maxDeque[maxDeque.length - 1].val <= peak) maxDeque.pop()
                    maxDeque.push({ val: peak, index: i })
                    if (i >= windowSize) {
                        const old = perSecond[i - windowSize]
                        sumRms -= old.rms; sumSqRms -= old.rms * old.rms
                        if (maxDeque[0].index <= i - windowSize) maxDeque.shift()
                    }
                    const count = Math.min(i + 1, windowSize)
                    const meanRms = sumRms / count
                    const variance = Math.max(0, (sumSqRms / count) - (meanRms * meanRms))
                    lines.push(`${ts},${rms.toFixed(4)},${peak.toFixed(4)},${bpm.toFixed(1)},${freq.toFixed(2)},${zcr.toFixed(4)},${meanRms.toFixed(4)},${variance.toFixed(6)},${maxDeque[0].val.toFixed(3)}`)
                }

                const blob = new Blob([lines.join('\n')], { type: 'text/csv' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url; a.download = `session_${id}.csv`
                document.body.appendChild(a); a.click(); a.remove()
                URL.revokeObjectURL(url)
            })
            .catch(err => console.error('Export failed', err))
    }, [archivedSessionId, currentSessionId, selectedSessionId, showFFT])

    // ── Derived ────────────────────────────────────────────────────────────
    const canExport = archivedSessionId || currentSessionId || (selectedSessionId && sessionStopped)
    const showInference = sessionStopped && (archivedSessionId || currentSessionId)
    const inferenceId = archivedSessionId || currentSessionId

    const formatSessionTime = useCallback(seconds => {
        const m = Math.floor(seconds / 60)
        const s = seconds % 60
        return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
    }, [])

    // ── Render ─────────────────────────────────────────────────────────────
    return (
        <div className="dashboard">

            <header className="header">
                <h1>Audio Waveform Analyzer</h1>
                <div className="header__right">
                    <ConnectionStatus isConnected={isConnected} status={connectionStatus} />
                    {liveLatency !== null && (
                        <div style={{ marginLeft: '12px', fontSize: '0.85rem', fontWeight: 'bold', color: liveLatency < 0.15 ? '#4caf50' : liveLatency < 0.3 ? '#ff9800' : '#f44336' }}>
                            LIVE LATENCY: {Math.round(liveLatency * 1000)}ms
                        </div>
                    )}
                    <HistoryIcon onClick={() => setSidebarOpen(open => !open)} isOpen={sidebarOpen} />
                </div>
                <SessionSidebar
                    isOpen={sidebarOpen}
                    onClose={() => setSidebarOpen(false)}
                    onSelectSession={handleLoadSession}
                />
            </header>

            <div className="main-content">

                {/* ── Live session picker sidebar ── */}
                <SessionListPanel
                    sessions={sessions}
                    selectedId={selectedSessionId}
                    onSelect={handleSelectSession}
                />

                <div className="waveform-panel">
                    <h2 className="panel-title">Real-Time Waveform</h2>
                    <div className="waveform-container">
                        <WaveformVisualization
                            key={waveformResetKey}
                            bufferInfo={{ buffer: waveformBufferRef.current, indexRef: waveformIndexRef }}
                            isConnected={isConnected}
                            isActiveSession={sessionActive}
                            showFFT={showFFT}
                        />
                    </div>
                </div>

                <div className="metrics-panel">
                    <MetricsPanel
                        metrics={currentMetrics}
                        sessionTime={formatSessionTime(sessionTime)}
                        showFFT={showFFT}
                    />
                </div>

                <div className="live-acoustic-panel-wrapper">
                    <LiveAcousticPanel
                        key={`live-panel-${waveformResetKey}`}
                        subscribe={subscribeRaw}
                        activeSessionId={activeSessionId}
                    />
                </div>

                {averages && (
                    <AverageMetricsPanel
                        averages={averages}
                        sessionId={currentSessionId}
                        animate={true}
                        showFFT={showFFT}
                    />
                )}

                {timeline && (
                    <TimelinePanel timeline={timeline} />
                )}

                {showInference && inferenceId && (
                    <InferenceAnalyticsPanel sessionId={inferenceId} />
                )}

            </div>

            <div className="controls-row" style={{ justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', gap: '12px' }}>
                    <select
                        className="view-select"
                        value={showFFT ? "fft" : "wave"}
                        onChange={(e) => setShowFFT(e.target.value === "fft")}
                    >
                        <option value="wave">Waveform View</option>
                        <option value="fft">FFT Spectrum View</option>
                    </select>
                    <button className="btn" onClick={handleRefreshWaveform}>Refresh Waveform</button>
                    <button className="btn btn-start" onClick={handleStartNewSession}>Start New Session</button>
                    <button
                        id="btn-desktop-audio"
                        className={`btn ${activeSessionId ? 'btn-stop' : 'btn-desktop'}`}
                        onClick={handleDesktopAudioClick}
                        title={activeSessionId ? 'Stop desktop audio streaming' : 'Stream desktop system audio via local helper'}
                    >
                        {activeSessionId ? '⏹ Stop Desktop Streaming' : '▶ Launch Desktop Streaming'}
                    </button>
                    <button className="btn btn-desktop" onClick={() => setShowDesktopModal(true)}>
                        Install Agent
                    </button>
                </div>
                <div style={{ display: 'flex', gap: '12px' }}>
                    <button className="btn btn-export-averages" onClick={handleExportAverages} disabled={!canExport}>Export Averages</button>
                    <button className="btn btn-export" onClick={handleExportSession} disabled={!canExport}>Export Metrics</button>
                    <button className="btn btn-stop" onClick={handleStopSession} disabled={!activeSessionId}>Stop Session</button>
                </div>
            </div>

            {showDesktopModal && (
                <DesktopStreamModal onClose={() => setShowDesktopModal(false)} />
            )}

        </div>
    )
}

export default App