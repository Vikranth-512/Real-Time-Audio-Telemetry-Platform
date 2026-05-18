import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import WaveformVisualization from './components/WaveformVisualization'
import MetricsPanel from './components/MetricsPanel'
import ConnectionStatus from './components/ConnectionStatus'
import AverageMetricsPanel from './components/AverageMetricsPanel'
import SessionSidebar, { HistoryIcon } from './components/SessionSidebar'
import LiveAcousticPanel from './components/LiveAcousticPanel'
import InferenceAnalyticsPanel from './components/InferenceAnalyticsPanel'
import './styles.css'
import { useWebSocket } from './hooks/useWebSocket'

// ─── Session list sidebar ─────────────────────────────────────────────────────
function SessionListPanel({ sessions, selectedId, onSelect }) {
    const entries = Object.values(sessions)
    if (entries.length === 0) return null

    return (
        <div className="session-list-panel">
            <div className="session-list-panel__title">Live Sessions</div>
            {entries.map(s => {
                const isSelected = s.sessionId === selectedId
                const isLive     = s.status === 'live'
                const rate       = s.packetRate ? `${s.packetRate.toFixed(1)} pkt/s` : ''
                const ago        = s.lastSeen
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
                            <span className="session-list-item__id">{s.sessionId}</span>
                        </div>
                        <div className="session-list-item__meta">
                            <span>{s.deviceId || 'device'}</span>
                            {rate && <span>{rate}</span>}
                            {ago  && <span>{ago}</span>}
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
    const [sessions, setSessions]               = useState({})
    const [selectedSessionId, setSelectedSessionId] = useState(null)

    // ── Per-selected-session UI state ──────────────────────────────────────
    const [currentMetrics, setCurrentMetrics]   = useState({
        bpm: 0, rms: 0, peak: 0, frequency: 0,
        peak_frequency: 0, spectral_centroid: 0, spectral_rolloff: 0, spectral_flatness: 0,
    })
    const [sessionTime, setSessionTime]         = useState(0)
    const [waveformData, setWaveformData]       = useState([])
    const [sessionStartTime, setSessionStartTime] = useState(null)
    const [sessionActive, setSessionActive]     = useState(false)
    const [sessionStopped, setSessionStopped]   = useState(false)
    const [averages, setAverages]               = useState(null)
    const [currentSessionId, setCurrentSessionId] = useState(null)
    const [archivedSessionId, setArchivedSessionId] = useState(null)

    // ── UI state ───────────────────────────────────────────────────────────
    const [sidebarOpen, setSidebarOpen]         = useState(false)
    const [showFFT, setShowFFT]                 = useState(false)
    const [waveformResetKey, setWaveformResetKey] = useState(0)

    const timerRef = useRef(null)

    const { isConnected, connectionStatus, send, subscribe, subscribeToSession, subscribeRaw } =
        useWebSocket('ws://localhost:8000/ws/audio')

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
                                sessionId:  s.session_id,
                                deviceId:   s.device_id || 'device',
                                status:     'live',
                                lastSeen:   null,
                                packetRate: 0,
                                _pktCount:  0,
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
                            sessionId:  sid,
                            deviceId:   data.device_id || 'device',
                            status:     'live',
                            lastSeen:   null,
                            packetRate: 0,
                            _pktCount:  0,
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
                setSelectedSessionId(prev => {
                    if (prev === sid) {
                        setSessionActive(false)
                        setSessionStopped(true)
                        setArchivedSessionId(sid)
                    }
                    return prev
                })
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
            setSessionActive(true)
            setSessionStopped(false)
            setArchivedSessionId(null)
            setAverages(null)
            setSessionStartTime(Date.now())
        }
    }, [sessions, selectedSessionId])

    // ── Track packet rate per session + update selectedSession UI state ────
    const lastPktTimes = useRef({}) // session_id -> circular ts array for rate calc

    useEffect(() => {
        if (!selectedSessionId) return

        const unsubSession = subscribeToSession(selectedSessionId, (data) => {
            if (data.type !== 'audio_update') return

            // Packet rate tracking (rolling 2s window)
            const now = Date.now() / 1000
            setSessions(prev => {
                const s = prev[selectedSessionId]
                if (!s) return prev
                const elapsed = now - (s._pktWindow / 1000)
                const count   = (s._pktCount || 0) + 1
                let rate      = s.packetRate
                if (elapsed >= 2) {
                    rate = count / elapsed
                    return { ...prev, [selectedSessionId]: { ...s, lastSeen: now, _pktCount: 0, _pktWindow: Date.now(), packetRate: rate } }
                }
                return { ...prev, [selectedSessionId]: { ...s, lastSeen: now, _pktCount: count } }
            })

            // Update waveform + metrics
            const metrics = data.metrics || {}
            setCurrentMetrics(m => ({
                ...m,
                bpm:  metrics.bpm  || 0,
                rms:  metrics.rms  || 0,
                peak: metrics.peak || 0,
                frequency: metrics.frequency || 0,
            }))

            if (!sessionStartTime) setSessionStartTime(Date.now())
            if (!sessionActive)    setSessionActive(true)

            const samples = data.samples || []
            if (samples.length > 0) {
                setWaveformData(prev => {
                    const next = [...prev, ...samples]
                    return next.length > 4096 ? next.slice(-4096) : next
                })
            }
        })

        return unsubSession
    }, [selectedSessionId, subscribeToSession, sessionStartTime, sessionActive])

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

    // ── Handlers ───────────────────────────────────────────────────────────

    const handleRefreshWaveform = useCallback(() => {
        setWaveformData([])
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
        setWaveformData([])
        setAverages(null)
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
        setWaveformData([])
        setSessionStartTime(null)
        setSessionTime(0)
        setWaveformResetKey(prev => prev + 1)
    }, [])

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
                setSessions(prev => ({
                    ...prev,
                    [sid]: { ...prev[sid], status: 'ended' }
                }))

                // Fetch averages for the stopped session
                fetch(`/api/session/${sid}/metrics?mode=${mode}`)
                    .then(r => r.json())
                    .then(data => {
                        setAverages(data.averages || {})
                        setCurrentSessionId(data.session_id)
                    })
                    .catch(err => console.error('Averages fetch failed', err))
            })
    }, [selectedSessionId, showFFT])

    const handleLoadSession = useCallback((sessionId) => {
        setArchivedSessionId(sessionId)
        setCurrentSessionId(sessionId)
        setSelectedSessionId(sessionId)
        setSessionStopped(true)
        setSessionActive(false)
        setSidebarOpen(false)
    }, [])

    const handleExportAverages = useCallback(() => {
        const id = archivedSessionId || currentSessionId || selectedSessionId
        if (!id) return
        const mode = showFFT ? 'fft' : 'wave'
        fetch(`/api/session/${id}/averages?mode=${mode}`)
            .then(r => r.json())
            .then(data => { setAverages(data); setCurrentSessionId(data.session_id || id) })
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
                const url  = URL.createObjectURL(blob)
                const a    = document.createElement('a')
                a.href = url; a.download = `session_${id}.csv`
                document.body.appendChild(a); a.click(); a.remove()
                URL.revokeObjectURL(url)
            })
            .catch(err => console.error('Export failed', err))
    }, [archivedSessionId, currentSessionId, selectedSessionId, showFFT])

    // ── Derived ────────────────────────────────────────────────────────────
    const canExport     = archivedSessionId || currentSessionId || (selectedSessionId && sessionStopped)
    const activeSessionId = selectedSessionId && sessions[selectedSessionId]?.status === 'live' ? selectedSessionId : null
    const showInference = sessionStopped && (archivedSessionId || currentSessionId)
    const inferenceId   = archivedSessionId || currentSessionId

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
                    <HistoryIcon onClick={() => setSidebarOpen(open => !open)} isOpen={sidebarOpen} />
                </div>
                <SessionSidebar
                    isOpen={sidebarOpen}
                    onClose={() => setSidebarOpen(false)}
                    onSelectSession={handleLoadSession}
                    showFFT={showFFT}
                    onToggleFFT={() => setShowFFT(v => !v)}
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
                            data={waveformData}
                            isConnected={isConnected}
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

                {showInference && inferenceId && (
                    <InferenceAnalyticsPanel sessionId={inferenceId} />
                )}

            </div>

            <div className="controls-row" style={{ justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', gap: '12px' }}>
                    <button className="btn" onClick={handleRefreshWaveform}>Refresh Waveform</button>
                    <button className="btn btn-start" onClick={handleStartNewSession}>Start New Session</button>
                </div>
                <div style={{ display: 'flex', gap: '12px' }}>
                    <button className="btn btn-export-averages" onClick={handleExportAverages} disabled={!canExport}>Export Averages</button>
                    <button className="btn btn-export"          onClick={handleExportSession}  disabled={!canExport}>Export Metrics</button>
                    <button className="btn btn-stop"            onClick={handleStopSession}    disabled={!activeSessionId}>Stop Session</button>
                </div>
            </div>

        </div>
    )
}

export default App