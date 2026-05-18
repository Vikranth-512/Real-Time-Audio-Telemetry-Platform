import React, { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import {
    runAnalytics,
    lttbDownsample,
    computeRollingStats,
} from '../utils/acousticAnalytics';
import {
    Chart,
    LineController,
    LineElement,
    PointElement,
    LinearScale,
    TimeScale,
    Tooltip,
    Legend,
    Filler,
    CategoryScale,
    ScatterController,
} from 'chart.js';

Chart.register(
    LineController, LineElement, PointElement, LinearScale,
    TimeScale, Tooltip, Legend, Filler, CategoryScale, ScatterController
);

// ─── Constants ────────────────────────────────────────────────────────────────
const MAX_CHART_POINTS = 300;
const SEVERITY_COLORS = {
    info:   { bg: 'rgba(77,163,255,0.15)',  border: 'rgba(77,163,255,0.5)',  text: '#6EC1FF' },
    low:    { bg: 'rgba(77,204,136,0.15)',  border: 'rgba(77,204,136,0.5)',  text: '#4DCC88' },
    medium: { bg: 'rgba(245,158,11,0.15)',  border: 'rgba(245,158,11,0.5)',  text: '#F59E0B' },
    high:   { bg: 'rgba(239,68,68,0.15)',   border: 'rgba(239,68,68,0.5)',   text: '#EF4444' },
};
const SEGMENT_COLORS = {
    'quiet':       '#1e3a5f',
    'stable':      '#0d5c5c',
    'active':      '#2d4a8a',
    'burst-heavy': '#7a4500',
    'chaotic':     '#6b1a1a',
};

// ─── Helper: format timestamp as MM:SS relative to session start ──────────────
function relLabel(ts, startTs) {
    const s = Math.round(ts - startTs);
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}:${sec.toString().padStart(2, '0')}`;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

const SummaryCard = ({ label, value, unit = '', sub = '' }) => (
    <div className="infer-card">
        <div className="infer-card__label">{label}</div>
        <div className="infer-card__value">
            {value}<span className="infer-card__unit">{unit}</span>
        </div>
        {sub && <div className="infer-card__sub">{sub}</div>}
    </div>
);

const AssumptionBadge = ({ a }) => {
    const col = SEVERITY_COLORS[a.severity] || SEVERITY_COLORS.info;
    return (
        <div className="infer-badge" style={{ background: col.bg, borderColor: col.border }}>
            <div className="infer-badge__header">
                <span className="infer-badge__label" style={{ color: col.text }}>{a.label}</span>
                <span className="infer-badge__conf" style={{ color: col.text }}>
                    {Math.round(parseFloat(a.confidence) * 100)}%
                </span>
            </div>
            <div className="infer-badge__desc">{a.description}</div>
        </div>
    );
};

const FingerprintChip = ({ label, value, color }) => (
    <div className="infer-chip" style={{ borderColor: color, color }}>
        <span className="infer-chip__label">{label}</span>
        <span className="infer-chip__value">{value}</span>
    </div>
);

const RadialGauge = ({ value, label, color = '#6EC1FF', size = 110 }) => {
    const r = 40;
    const circ = 2 * Math.PI * r;
    const pct = Math.min(Math.max(value, 0), 100) / 100;
    const dash = pct * circ * 0.75; // 270° sweep
    const gap = circ - dash;
    const rotation = -135; // start at bottom-left

    return (
        <div className="infer-gauge">
            <svg width={size} height={size} viewBox="0 0 100 100">
                <circle cx="50" cy="50" r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="9"
                    strokeDasharray={`${circ * 0.75} ${circ * 0.25}`}
                    transform={`rotate(${rotation} 50 50)`} />
                <circle cx="50" cy="50" r={r} fill="none" stroke={color} strokeWidth="9"
                    strokeLinecap="round"
                    strokeDasharray={`${dash} ${gap + circ * 0.25}`}
                    transform={`rotate(${rotation} 50 50)`}
                    style={{ transition: 'stroke-dasharray 0.6s ease' }} />
                <text x="50" y="52" textAnchor="middle" dominantBaseline="middle"
                    fill="#E8F1FF" fontSize="16" fontWeight="600">{value}</text>
            </svg>
            <div className="infer-gauge__label">{label}</div>
        </div>
    );
};

const SessionTimeline = ({ segments, startTs, endTs }) => {
    const total = endTs - startTs || 1;
    return (
        <div className="infer-timeline">
            {segments.map((seg, i) => {
                const left = ((seg.start - startTs) / total) * 100;
                const width = ((seg.end - seg.start) / total) * 100;
                return (
                    <div
                        key={i}
                        className="infer-timeline__seg"
                        style={{
                            left: `${left}%`,
                            width: `${Math.max(width, 0.5)}%`,
                            background: SEGMENT_COLORS[seg.type] || '#1a2a4a',
                        }}
                        title={`${seg.type} (${Math.round(seg.end - seg.start)}s)`}
                    >
                        <span className="infer-timeline__seg-label">{seg.type}</span>
                    </div>
                );
            })}
            {segments.length === 0 && (
                <div className="infer-timeline__empty">Insufficient data for segmentation</div>
            )}
        </div>
    );
};

const HistogramCanvas = ({ data, label, color = '#6EC1FF', bins = 20 }) => {
    const canvasRef = useRef(null);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas || !data || data.length === 0) return;

        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        canvas.width = canvas.clientWidth * dpr;
        canvas.height = canvas.clientHeight * dpr;
        ctx.scale(dpr, dpr);

        const W = canvas.clientWidth;
        const H = canvas.clientHeight;
        ctx.clearRect(0, 0, W, H);

        // Build bins in O(N)
        let min = Infinity, max = -Infinity;
        for (const v of data) { if (v < min) min = v; if (v > max) max = v; }
        if (min === max) max = min + 1;

        const counts = new Array(bins).fill(0);
        const range = max - min;
        for (const v of data) {
            const b = Math.min(Math.floor(((v - min) / range) * bins), bins - 1);
            counts[b]++;
        }

        const maxCount = Math.max(...counts);
        const barW = W / bins;
        const pad = 1;

        ctx.fillStyle = color.replace(')', ',0.15)').replace('rgb', 'rgba');

        for (let i = 0; i < bins; i++) {
            const barH = (counts[i] / maxCount) * (H - 8);
            const x = i * barW + pad;
            const y = H - barH - 4;

            // Gradient fill
            const grad = ctx.createLinearGradient(0, y, 0, H);
            grad.addColorStop(0, color.includes('rgba') ? color : color + 'cc');
            grad.addColorStop(1, color.includes('rgba') ? color.replace(/[\d.]+\)$/, '0.05)') : color + '11');
            ctx.fillStyle = grad;
            ctx.beginPath();
            ctx.roundRect(x, y, barW - pad * 2, barH, 2);
            ctx.fill();
        }

        // X-axis labels
        ctx.fillStyle = 'rgba(168,195,230,0.5)';
        ctx.font = `${9 * dpr / dpr}px sans-serif`;
        ctx.textAlign = 'left';
        ctx.fillText(min.toFixed(3), 2, H - 1);
        ctx.textAlign = 'right';
        ctx.fillText(max.toFixed(3), W - 2, H - 1);
    }, [data, color, bins]);

    return (
        <div className="infer-histogram">
            <div className="infer-histogram__label">{label}</div>
            <canvas ref={canvasRef} style={{ width: '100%', height: '80px', display: 'block' }} />
        </div>
    );
};

// Chart.js line chart with dual y-axes and spike overlay
const MultiMetricChart = ({ downsampled, spikes, startTs }) => {
    const canvasRef = useRef(null);
    const chartRef = useRef(null);

    useEffect(() => {
        if (!canvasRef.current || !downsampled || downsampled.length === 0) return;

        if (chartRef.current) {
            chartRef.current.destroy();
            chartRef.current = null;
        }

        const labels = downsampled.map(p => relLabel(p.timestamp, startTs));
        const spikeSet = new Set(spikes.map(s => s.timestamp));

        chartRef.current = new Chart(canvasRef.current, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {
                        label: 'RMS',
                        data: downsampled.map(p => p.rms),
                        borderColor: 'rgba(110,193,255,0.9)',
                        backgroundColor: 'rgba(110,193,255,0.07)',
                        borderWidth: 1.8,
                        pointRadius: downsampled.map(p => spikeSet.has(p.timestamp) ? 5 : 0),
                        pointBackgroundColor: downsampled.map(p =>
                            spikeSet.has(p.timestamp) ? '#EF4444' : 'transparent'
                        ),
                        pointBorderColor: 'transparent',
                        fill: true,
                        tension: 0.3,
                        yAxisID: 'yEnergy',
                    },
                    {
                        label: 'Amplitude',
                        data: downsampled.map(p => p.peak),
                        borderColor: 'rgba(255,120,120,0.75)',
                        borderWidth: 1.5,
                        pointRadius: 0,
                        fill: false,
                        tension: 0.3,
                        yAxisID: 'yEnergy',
                    },
                    {
                        label: 'ZCR',
                        data: downsampled.map(p => p.zcr),
                        borderColor: 'rgba(120,255,160,0.6)',
                        borderWidth: 1.2,
                        pointRadius: 0,
                        fill: false,
                        tension: 0.3,
                        yAxisID: 'yZcr',
                    },
                ],
            },
            options: {
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        labels: { color: '#A8C3E6', font: { size: 11 } }
                    },
                    tooltip: {
                        backgroundColor: 'rgba(10,25,50,0.92)',
                        titleColor: '#6EC1FF',
                        bodyColor: '#A8C3E6',
                        borderColor: 'rgba(110,193,255,0.3)',
                        borderWidth: 1,
                    }
                },
                scales: {
                    x: {
                        ticks: {
                            color: '#5A7FA8',
                            maxTicksLimit: 10,
                            font: { size: 10 },
                        },
                        grid: { color: 'rgba(255,255,255,0.04)' },
                    },
                    yEnergy: {
                        type: 'linear',
                        position: 'left',
                        min: 0, max: 1,
                        title: { display: true, text: 'Energy (0–1)', color: '#5A7FA8', font: { size: 10 } },
                        ticks: { color: '#5A7FA8', font: { size: 10 } },
                        grid: { color: 'rgba(255,255,255,0.04)' },
                    },
                    yZcr: {
                        type: 'linear',
                        position: 'right',
                        min: 0, max: 0.5,
                        title: { display: true, text: 'ZCR', color: '#5A7FA8', font: { size: 10 } },
                        ticks: { color: '#5A7FA8', font: { size: 10 } },
                        grid: { drawOnChartArea: false },
                    },
                },
            },
        });

        return () => {
            if (chartRef.current) {
                chartRef.current.destroy();
                chartRef.current = null;
            }
        };
    }, [downsampled, spikes, startTs]);

    return (
        <div style={{ position: 'relative', height: '220px', width: '100%' }}>
            <canvas ref={canvasRef} />
        </div>
    );
};

const FreqDriftChart = ({ rawMetrics, freqEma, startTs }) => {
    const canvasRef = useRef(null);
    const chartRef = useRef(null);

    useEffect(() => {
        if (!canvasRef.current || !rawMetrics || rawMetrics.length === 0) return;
        if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }

        const freqPoints = rawMetrics.filter(m => m.frequency > 20);
        if (freqPoints.length < 3) return;

        const dsPoints = lttbDownsample(
            freqPoints.map(p => ({ x: p.timestamp, y: p.frequency })),
            MAX_CHART_POINTS
        );
        const emaDs = lttbDownsample(
            freqPoints.map((p, i) => ({ x: p.timestamp, y: freqEma[i] || p.frequency })),
            MAX_CHART_POINTS
        );

        const labels = dsPoints.map(p => relLabel(p.x, startTs));

        chartRef.current = new Chart(canvasRef.current, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    {
                        label: 'Frequency (Hz)',
                        data: dsPoints.map(p => p.y),
                        borderColor: 'rgba(180,130,255,0.7)',
                        borderWidth: 1.5,
                        pointRadius: 0,
                        fill: false,
                        tension: 0.4,
                    },
                    {
                        label: 'EMA Trend',
                        data: emaDs.map(p => p.y),
                        borderColor: 'rgba(255,200,80,0.9)',
                        borderWidth: 2,
                        borderDash: [4, 4],
                        pointRadius: 0,
                        fill: false,
                        tension: 0.4,
                    },
                ],
            },
            options: {
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { labels: { color: '#A8C3E6', font: { size: 11 } } },
                    tooltip: {
                        backgroundColor: 'rgba(10,25,50,0.92)',
                        titleColor: '#B482FF',
                        bodyColor: '#A8C3E6',
                        borderColor: 'rgba(180,130,255,0.3)',
                        borderWidth: 1,
                    },
                },
                scales: {
                    x: {
                        ticks: { color: '#5A7FA8', maxTicksLimit: 8, font: { size: 10 } },
                        grid: { color: 'rgba(255,255,255,0.04)' },
                    },
                    y: {
                        title: { display: true, text: 'Frequency (Hz)', color: '#5A7FA8', font: { size: 10 } },
                        ticks: { color: '#5A7FA8', font: { size: 10 } },
                        grid: { color: 'rgba(255,255,255,0.04)' },
                    },
                },
            },
        });

        return () => { if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; } };
    }, [rawMetrics, freqEma, startTs]);

    return (
        <div style={{ position: 'relative', height: '180px', width: '100%' }}>
            <canvas ref={canvasRef} />
        </div>
    );
};

// ─── Main Panel ───────────────────────────────────────────────────────────────

const InferenceAnalyticsPanel = ({ sessionId }) => {
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [rawMetrics, setRawMetrics] = useState(null);
    const [analytics, setAnalytics] = useState(null);

    // Cache analytics by sessionId to avoid recomputation
    const analyticsCache = useRef(new Map());

    // Fetch once on mount
    useEffect(() => {
        if (!sessionId) return;
        setLoading(true);
        setError(null);
        setRawMetrics(null);
        setAnalytics(null);

        fetch(`/api/session/${sessionId}/metrics`)
            .then(r => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then(data => {
                const rows = data.full_metrics || [];
                const normalized = rows.map(r => ({
                    timestamp: r.timestamp,
                    rms: r.metrics.rms || 0,
                    peak: r.metrics.peak || 0,
                    zcr: r.metrics.zcr || 0,
                    frequency: r.metrics.frequency || 0,
                }));
                setRawMetrics(normalized);
                setLoading(false);
            })
            .catch(err => {
                setError(err.message);
                setLoading(false);
            });
    }, [sessionId]);

    // Compute analytics via requestIdleCallback to avoid blocking mount
    useEffect(() => {
        if (!rawMetrics) return;

        if (analyticsCache.current.has(sessionId)) {
            setAnalytics(analyticsCache.current.get(sessionId));
            return;
        }

        const compute = () => {
            const result = runAnalytics(rawMetrics);
            analyticsCache.current.set(sessionId, result);
            setAnalytics(result);
        };

        if ('requestIdleCallback' in window) {
            const id = requestIdleCallback(compute, { timeout: 600 });
            return () => cancelIdleCallback(id);
        } else {
            const id = setTimeout(compute, 0);
            return () => clearTimeout(id);
        }
    }, [rawMetrics, sessionId]);

    // Downsampled points for charts — memoized, never recomputed
    const downsampled = useMemo(() => {
        if (!rawMetrics || rawMetrics.length === 0) return [];
        const pts = rawMetrics.map(m => ({ x: m.timestamp, ...m }));
        return lttbDownsample(pts, MAX_CHART_POINTS);
    }, [rawMetrics]);

    const startTs = rawMetrics && rawMetrics.length > 0 ? rawMetrics[0].timestamp : 0;
    const endTs = rawMetrics && rawMetrics.length > 0 ? rawMetrics[rawMetrics.length - 1].timestamp : 0;
    const durationSec = Math.round(endTs - startTs);

    if (loading) {
        return (
            <div className="inference-panel inference-panel--loading">
                <div className="inference-panel__spinner" />
                <span>Computing session intelligence...</span>
            </div>
        );
    }

    if (error) {
        return (
            <div className="inference-panel inference-panel--error">
                ⚠ Failed to load session analytics: {error}
            </div>
        );
    }

    if (!analytics || !rawMetrics) return null;

    const { assumptions, fingerprint, spikeResult, quietResult, freqDrift, segments,
        activityScore, stabilityScore, tonality, rmsEma, freqEma } = analytics;

    const avgRms = rawMetrics.reduce((s, m) => s + m.rms, 0) / rawMetrics.length;
    const peakAmp = rawMetrics.reduce((max, m) => Math.max(max, m.peak), 0);
    const avgFreq = rawMetrics.filter(m => m.frequency > 20).reduce((s, m) => s + m.frequency, 0) /
        Math.max(rawMetrics.filter(m => m.frequency > 20).length, 1);
    const avgZcr = rawMetrics.reduce((s, m) => s + m.zcr, 0) / rawMetrics.length;

    const rmsData = rawMetrics.map(m => m.rms);
    const peakData = rawMetrics.map(m => m.peak);
    const zcrData = rawMetrics.map(m => m.zcr);

    const fingerprintColors = {
        dominantPattern: '#6EC1FF',
        tonality: '#B482FF',
        stabilityClass: '#4DCC88',
        activityClass: '#F59E0B',
        spikeProfile: '#F87171',
        driftProfile: '#60A5FA',
    };

    return (
        <section className="inference-panel" aria-label="Post-session acoustic analytics">
            {/* ── Header ── */}
            <div className="inference-panel__header">
                <div>
                    <h2 className="panel-title inference-panel__title">Session Intelligence Report</h2>
                    <p className="inference-panel__sub">
                        Session {sessionId?.slice(0, 8)}… · {durationSec}s · {rawMetrics.length} samples
                    </p>
                </div>
            </div>

            {/* ── Fingerprint Chips ── */}
            <div className="infer-chips-row">
                {Object.entries(fingerprint).map(([k, v]) => (
                    <FingerprintChip
                        key={k}
                        label={k.replace(/([A-Z])/g, ' $1').toLowerCase()}
                        value={v}
                        color={fingerprintColors[k] || '#6EC1FF'}
                    />
                ))}
            </div>

            {/* ── Summary Cards ── */}
            <div className="infer-summary-grid">
                <SummaryCard label="Avg RMS"         value={avgRms.toFixed(3)} />
                <SummaryCard label="Peak Amplitude"  value={peakAmp.toFixed(3)} />
                <SummaryCard label="Avg Frequency"   value={avgFreq.toFixed(0)} unit=" Hz" />
                <SummaryCard label="Avg ZCR"         value={avgZcr.toFixed(4)} />
                <SummaryCard label="Spike Count"     value={spikeResult.spikes.length} />
                <SummaryCard label="Quiet Time"      value={quietResult.idlePercent} unit="%" />
                <SummaryCard label="Session Length"  value={durationSec} unit="s" />
                <SummaryCard label="Tonality"        value={tonality} />
            </div>

            {/* ── Gauges ── */}
            <div className="infer-gauges-row">
                <RadialGauge value={activityScore} label="Activity Score" color="#F59E0B" />
                <RadialGauge value={stabilityScore} label="Stability Score" color="#4DCC88" />
                <div className="infer-gauge-spacer">
                    <div className="infer-drift-info">
                        <span className="infer-drift-label">Frequency Drift</span>
                        <span className="infer-drift-value" style={{
                            color: freqDrift.stabilityLabel === 'stable' ? '#4DCC88' :
                                   freqDrift.stabilityLabel === 'drifting' ? '#F59E0B' : '#EF4444'
                        }}>
                            {freqDrift.stabilityLabel}
                        </span>
                        <span className="infer-drift-sub">slope: {freqDrift.driftSlope.toFixed(3)} Hz/sample</span>
                        {spikeResult.burstDensity > 0 && (
                            <span className="infer-drift-sub">burst density: {spikeResult.burstDensity.toFixed(2)}/s</span>
                        )}
                    </div>
                </div>
            </div>

            {/* ── Session Timeline ── */}
            <div className="inference-section">
                <h3 className="inference-section__title">Session Timeline</h3>
                <SessionTimeline segments={segments} startTs={startTs} endTs={endTs} />
                <div className="infer-timeline__legend">
                    {Object.entries(SEGMENT_COLORS).map(([type, color]) => (
                        <span key={type} className="infer-timeline__legend-item">
                            <span className="infer-timeline__legend-dot" style={{ background: color }} />
                            {type}
                        </span>
                    ))}
                </div>
            </div>

            {/* ── Assumptions ── */}
            <div className="inference-section">
                <h3 className="inference-section__title">Acoustic Intelligence Observations</h3>
                <div className="infer-badges-grid">
                    {assumptions.map((a, i) => <AssumptionBadge key={i} a={a} />)}
                </div>
            </div>

            {/* ── Multi-Metric Chart ── */}
            <div className="inference-section">
                <h3 className="inference-section__title">Signal Overview · RMS / Amplitude / ZCR</h3>
                <p className="inference-section__hint">Red dots indicate detected acoustic spikes</p>
                <MultiMetricChart
                    downsampled={downsampled}
                    spikes={spikeResult.spikes}
                    startTs={startTs}
                />
            </div>

            {/* ── Frequency Drift ── */}
            <div className="inference-section">
                <h3 className="inference-section__title">Frequency Drift Analysis</h3>
                <p className="inference-section__hint">Gold dashed line = EMA trendline</p>
                <FreqDriftChart
                    rawMetrics={rawMetrics}
                    freqEma={freqEma || []}
                    startTs={startTs}
                />
            </div>

            {/* ── Histograms ── */}
            <div className="inference-section">
                <h3 className="inference-section__title">Distribution Analysis</h3>
                <div className="infer-histograms-row">
                    <HistogramCanvas data={rmsData}  label="RMS Distribution"       color="rgba(110,193,255,0.85)" />
                    <HistogramCanvas data={peakData} label="Amplitude Distribution"  color="rgba(255,120,120,0.85)" />
                    <HistogramCanvas data={zcrData}  label="ZCR Distribution"        color="rgba(120,255,160,0.85)" />
                </div>
            </div>
        </section>
    );
};

export default InferenceAnalyticsPanel;
