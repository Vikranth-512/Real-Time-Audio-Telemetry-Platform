import React, { useState, useMemo } from 'react';

// ── Formatters ─────────────────────────────────────────────────────────────
const formatDuration = (seconds) => {
    if (!seconds && seconds !== 0) return '0s';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m >= 60) {
        const h = Math.floor(m / 60);
        const rm = m % 60;
        return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
    }
    if (s === 0) return `${m}m`;
    return `${m}m ${s}s`;
};

const formatTimestamp = (seconds) => {
    if (seconds >= 3600) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60).toString().padStart(2, '0');
        return `${h}:${m.toString().padStart(2, '0')}:${s}`;
    }
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
};

// ── State colors (subdued observability palette) ───────────────────────────
const STATE_COLORS = {
    steady: 'rgba(110,193,255,0.30)',    // muted blue
    active: 'rgba(255,183,77,0.45)',     // muted amber
    burst: 'rgba(239,83,80,0.45)',      // muted red
    quiet: 'rgba(255,255,255,0.06)',    // near-invisible
};

const STATE_LEGEND = [
    { name: 'Steady', color: STATE_COLORS.steady },
    { name: 'Active', color: STATE_COLORS.active },
    { name: 'Burst', color: STATE_COLORS.burst },
    { name: 'Quiet', color: STATE_COLORS.quiet },
];

const getSegmentColor = (type) => STATE_COLORS[type] || STATE_COLORS.steady;

// ── Anomaly severity styling ───────────────────────────────────────────────
const SEVERITY_LABELS = {
    minor: 'Minor',
    major: 'Major',
    critical: 'Critical',
};

// ── Component ──────────────────────────────────────────────────────────────
const TimelinePanel = React.memo(({ timeline }) => {
    const [collapsed, setCollapsed] = useState(false);

    // Memoize sorted top anomalies
    const topAnomalies = useMemo(() => {
        if (!timeline?.anomalies?.length) return [];
        return timeline.anomalies
            .slice()
            .sort((a, b) => b.score - a.score)
            .slice(0, 8);
    }, [timeline?.anomalies]);

    if (!timeline || !timeline.duration) return null;

    const { baseline, duration, distribution, segments, events, anomalies, phases, insights, summary } = timeline;
    const barSegments = (segments && segments.length > 0) ? segments : [];

    return (
        <section className="timeline-panel" aria-label="Session Activity Timeline">
            <div className="timeline-panel__header">
                <h2 className="panel-title timeline-panel__title">Session Activity Timeline</h2>
                <button
                    className="timeline-panel__collapse-btn"
                    onClick={() => setCollapsed(c => !c)}
                    aria-label={collapsed ? 'Expand timeline' : 'Collapse timeline'}
                >
                    {collapsed ? '▸ Show' : '▾ Hide'}
                </button>
            </div>

            {!collapsed && (
                <>
                    {/* ── Insight Chips ── */}
                    <div className="timeline-panel__insights">
                        <div className="timeline-insight-chip">
                            <span className="timeline-insight-chip__label">Baseline Activity</span>
                            <span className="timeline-insight-chip__value">{baseline.rms} RMS</span>
                            <span className="timeline-insight-chip__subtext">
                                {insights.activity_volatility === 'Low' ? 'Stable' : 'Variable'} (±{baseline.variability})
                            </span>
                        </div>
                        <div className="timeline-insight-chip">
                            <span className="timeline-insight-chip__label">Session Dynamics</span>
                            <span className="timeline-insight-chip__value">{insights.session_dynamics}</span>
                        </div>
                        <div className="timeline-insight-chip">
                            <span className="timeline-insight-chip__label">Longest Stable Period</span>
                            <span className="timeline-insight-chip__value">{formatDuration(insights.longest_stable_period)}</span>
                        </div>
                        <div className="timeline-insight-chip">
                            <span className="timeline-insight-chip__label">Activity Volatility</span>
                            <span className="timeline-insight-chip__value">{insights.activity_volatility}</span>
                        </div>
                        <div className="timeline-insight-chip">
                            <span className="timeline-insight-chip__label">Anomalies</span>
                            <span className="timeline-insight-chip__value">{insights.anomaly_count} detected</span>
                        </div>
                        <div className="timeline-insight-chip">
                            <span className="timeline-insight-chip__label">Trend</span>
                            <span className="timeline-insight-chip__value">{insights.trend}</span>
                        </div>
                    </div>

                    {/* ── Activity Distribution Bar ── */}
                    {distribution && (
                        <div className="timeline-distribution">
                            <h3 className="timeline-section-title">Activity Distribution</h3>
                            <div className="timeline-distribution__bar">
                                {distribution.steady_pct > 0 && (
                                    <div className="timeline-distribution__segment" style={{ width: `${distribution.steady_pct}%`, backgroundColor: STATE_COLORS.steady }} title={`Steady: ${distribution.steady_pct}%`}>
                                        {distribution.steady_pct >= 8 && <span>{distribution.steady_pct}%</span>}
                                    </div>
                                )}
                                {distribution.active_pct > 0 && (
                                    <div className="timeline-distribution__segment" style={{ width: `${distribution.active_pct}%`, backgroundColor: STATE_COLORS.active }} title={`Active: ${distribution.active_pct}%`}>
                                        {distribution.active_pct >= 8 && <span>{distribution.active_pct}%</span>}
                                    </div>
                                )}
                                {distribution.burst_pct > 0 && (
                                    <div className="timeline-distribution__segment" style={{ width: `${distribution.burst_pct}%`, backgroundColor: STATE_COLORS.burst }} title={`Burst: ${distribution.burst_pct}%`}>
                                        {distribution.burst_pct >= 8 && <span>{distribution.burst_pct}%</span>}
                                    </div>
                                )}
                                {distribution.quiet_pct > 0 && (
                                    <div className="timeline-distribution__segment" style={{ width: `${distribution.quiet_pct}%`, backgroundColor: STATE_COLORS.quiet }} title={`Quiet: ${distribution.quiet_pct}%`}>
                                        {distribution.quiet_pct >= 8 && <span>{distribution.quiet_pct}%</span>}
                                    </div>
                                )}
                            </div>
                            <div className="timeline-distribution__labels">
                                <span>Steady {distribution.steady_pct}%</span>
                                <span>Active {distribution.active_pct}%</span>
                                <span>Burst {distribution.burst_pct}%</span>
                                <span>Quiet {distribution.quiet_pct}%</span>
                            </div>
                        </div>
                    )}

                    {/* ── Visual Timeline ── */}
                    <div className="timeline-visual-container">
                        <div className="timeline-visual__labels">
                            <span>{formatTimestamp(0)}</span>
                            <span>{formatTimestamp(duration)}</span>
                        </div>
                        <div className="timeline-visual__bar">
                            {barSegments.map((seg, idx) => {
                                const widthPct = (seg.duration / duration) * 100;
                                return (
                                    <div
                                        key={`seg-${idx}`}
                                        className="timeline-visual__phase"
                                        style={{
                                            width: `${widthPct}%`,
                                            backgroundColor: getSegmentColor(seg.type),
                                        }}
                                        title={`${seg.type} (${formatTimestamp(seg.start)} – ${formatTimestamp(seg.end)})`}
                                    />
                                );
                            })}

                            {/* Event markers */}
                            {events.map((event, idx) => {
                                const leftPct = (event.timestamp / duration) * 100;
                                return (
                                    <div
                                        key={`event-${idx}`}
                                        className={`timeline-visual__marker timeline-visual__marker--${event.type}`}
                                        style={{ left: `${leftPct}%` }}
                                    >
                                        <div className="timeline-visual__tooltip">
                                            <strong>{formatTimestamp(event.timestamp)}</strong><br />
                                            {event.description}
                                        </div>
                                    </div>
                                );
                            })}

                            {/* Anomaly markers */}
                            {anomalies.map((anom, idx) => {
                                const leftPct = (anom.timestamp / duration) * 100;
                                return (
                                    <div
                                        key={`anom-marker-${idx}`}
                                        className={`timeline-visual__marker timeline-visual__marker--anomaly-${anom.severity}`}
                                        style={{ left: `${leftPct}%` }}
                                    >
                                        <div className="timeline-visual__tooltip">
                                            <strong>{formatTimestamp(anom.timestamp)}</strong><br />
                                            {SEVERITY_LABELS[anom.severity] || 'Anomaly'} Incident (Score: {anom.score}σ)
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                        <div className="timeline-visual__legend">
                            {STATE_LEGEND.map(l => (
                                <div key={l.name} className="timeline-visual__legend-item">
                                    <span className="timeline-visual__legend-dot" style={{ backgroundColor: l.color }} />
                                    <span>{l.name}</span>
                                </div>
                            ))}
                            <div className="timeline-visual__legend-item">
                                <span className="timeline-visual__legend-dot timeline-visual__legend-dot--marker" />
                                <span>Event</span>
                            </div>
                        </div>
                    </div>

                    {/* ── Observability Metrics Strip ── */}
                    <div className="timeline-obs-strip">
                        <div className="timeline-obs-strip__item">
                            <span className="timeline-obs-strip__label">Confidence</span>
                            <span className="timeline-obs-strip__val">{Math.round((insights.timeline_confidence ?? 0) * 100)}%</span>
                        </div>
                        <span className="timeline-obs-strip__sep">·</span>
                        <div className="timeline-obs-strip__item">
                            <span className="timeline-obs-strip__label">Intensity</span>
                            <span className="timeline-obs-strip__val">{insights.activity_intensity_index ?? 0}/100</span>
                        </div>
                        <span className="timeline-obs-strip__sep">·</span>
                        <div className="timeline-obs-strip__item">
                            <span className="timeline-obs-strip__label">Drift</span>
                            <span className={`timeline-obs-strip__val ${(insights.baseline_drift_pct ?? 0) > 10 ? 'timeline-obs-strip__val--up' : (insights.baseline_drift_pct ?? 0) < -10 ? 'timeline-obs-strip__val--down' : ''}`}>
                                {(insights.baseline_drift_pct ?? 0) > 0 ? '+' : ''}{insights.baseline_drift_pct ?? 0}%
                            </span>
                        </div>
                        <span className="timeline-obs-strip__sep">·</span>
                        <div className="timeline-obs-strip__item">
                            <span className="timeline-obs-strip__label">Profile</span>
                            <span className="timeline-obs-strip__val">{insights.session_fingerprint ?? '—'}</span>
                        </div>
                    </div>

                    {/* ── Narrative Summary ── */}
                    <div className="timeline-summary">
                        <div className="timeline-summary__baseline">
                            <span className="timeline-summary__baseline-label">COMPUTED BASELINE</span>
                            <span className="timeline-summary__baseline-value">
                                {baseline?.rms || '0.00'} <span className="timeline-summary__baseline-unit">RMS</span>
                            </span>
                        </div>
                        <p>{summary}</p>
                    </div>

                    {/* ── Session Phases ── */}
                    {phases && phases.length > 0 && (
                        <div className="timeline-phases-container">
                            <h3 className="timeline-section-title">Session Phases</h3>
                            <div className="timeline-phases-grid">
                                {phases.map((phase, idx) => (
                                    <div key={`phase-${idx}`} className="timeline-phase-card">
                                        <div className="timeline-phase-card__time">
                                            {formatTimestamp(phase.start)} → {formatTimestamp(phase.end)}
                                        </div>
                                        <div className="timeline-phase-card__name">{phase.name}</div>
                                        <div className="timeline-phase-card__duration">{formatDuration(phase.duration)}</div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* ── Events ── */}
                    {events.length > 0 && (
                        <div className="timeline-events-container">
                            <h3 className="timeline-section-title">Activity Events</h3>
                            <div className="timeline-events-grid">
                                {events.map((event, idx) => (
                                    <div key={`ev-${idx}`} className={`timeline-event-card timeline-event-card--${event.type}`}>
                                        <div className="timeline-event-card__time">{formatTimestamp(event.timestamp)}</div>
                                        <div className="timeline-event-card__title">{event.description}</div>
                                        {event.duration && (
                                            <div className="timeline-event-card__score">Duration: {formatDuration(event.duration)}</div>
                                        )}
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}

                    {/* ── Anomalies (Incident-style) ── */}
                    {topAnomalies.length > 0 && (
                        <div className="timeline-events-container">
                            <h3 className="timeline-section-title">Anomaly Incidents ({anomalies.length} total)</h3>
                            <div className="timeline-events-grid">
                                {topAnomalies.map((anom, idx) => (
                                    <div key={`anom-${idx}`} className={`timeline-event-card timeline-event-card--anomaly-${anom.severity}`}>
                                        <div className="timeline-event-card__time">
                                            {formatTimestamp(anom.timestamp)}
                                            {anom.end != null && anom.end !== anom.timestamp && ` – ${formatTimestamp(anom.end)}`}
                                        </div>
                                        <div className="timeline-event-card__title">{SEVERITY_LABELS[anom.severity] || 'Anomaly'} Incident</div>
                                        <div className="timeline-event-card__score">Peak: {anom.score}σ{anom.avg_score != null && ` · Avg: ${anom.avg_score}σ`}</div>
                                        {anom.duration != null && anom.duration > 0 && (
                                            <div className="timeline-event-card__score">Duration: {formatDuration(anom.duration)}</div>
                                        )}
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </>
            )}
        </section>
    );
});

export default TimelinePanel;
