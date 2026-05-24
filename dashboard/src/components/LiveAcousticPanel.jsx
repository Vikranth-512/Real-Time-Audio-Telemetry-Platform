import React, { useEffect, useRef, useState } from 'react';
import { evaluateAssumptions, getTimeContext } from './assumptions';
import LiveMetricsChart from './LiveMetricsChart';

const MAX_POINTS = 512;

const LiveAcousticPanel = ({ subscribe, activeSessionId }) => {
    // Refs for raw data ingestion
    const rmsBuffer = useRef(new Float32Array(MAX_POINTS));
    const ampBuffer = useRef(new Float32Array(MAX_POINTS));
    const zcrBuffer = useRef(new Float32Array(MAX_POINTS));
    const bufferIndex = useRef(0);
    const lastAssumptionsTime = useRef(0);
    const hasData = useRef(false);
    
    // Fix stale closure in loop
    const activeSessionRef = useRef(activeSessionId);
    useEffect(() => {
        activeSessionRef.current = activeSessionId;
    }, [activeSessionId]);
    
    // Component state for UI
    const [assumptions, setAssumptions] = useState([]);
    const [stats, setStats] = useState({ meanRms: 0, variance: 0, peak: 0, zcr: 0 });

    useEffect(() => {
        if (!subscribe) return;
        
        const unsubscribe = subscribe((raw) => {
            try {
                const parsed = JSON.parse(raw);
                if (parsed.type === 'audio_update' && (!activeSessionId || parsed.session_id === activeSessionId)) {
                    hasData.current = true;
                    const metrics = parsed.metrics || {};
                    const idx = bufferIndex.current;
                    
                    rmsBuffer.current[idx] = metrics.rms || 0;
                    ampBuffer.current[idx] = metrics.peak || 0;
                    zcrBuffer.current[idx] = metrics.zcr || metrics.spectral_flatness || 0;
                    
                    bufferIndex.current = (idx + 1) % MAX_POINTS;
                }
            } catch (e) {
                // ignore parsing errors
            }
        });
        return unsubscribe;
    }, [subscribe, activeSessionId]);

    // Loop for assumptions (5-10Hz)
    useEffect(() => {
        let rafId;
        const loop = (timestamp) => {
            rafId = requestAnimationFrame(loop);
            
            // Limit to ~10 Hz (100ms)
            if (timestamp - lastAssumptionsTime.current > 100) {
                lastAssumptionsTime.current = timestamp;
                
                const idx = bufferIndex.current;
                const windowSize = 250; // Larger window for slower, aggregate assumption changes
                
                let sumRms = 0;
                let sumSq = 0;
                let maxAmp = 0;
                let sumZcr = 0;
                
                for (let i = 0; i < windowSize; i++) {
                    const pos = (idx - 1 - i + MAX_POINTS) % MAX_POINTS;
                    const val = rmsBuffer.current[pos];
                    sumRms += val;
                    sumSq += val * val;
                    
                    const ampVal = ampBuffer.current[pos];
                    if (ampVal > maxAmp) maxAmp = ampVal;
                    
                    sumZcr += zcrBuffer.current[pos];
                }
                
                const meanRms = sumRms / windowSize;
                const variance = Math.max(0, (sumSq / windowSize) - (meanRms * meanRms));
                const zcr = sumZcr / windowSize;
                
                let prevSumRms = 0;
                for (let i = windowSize; i < windowSize * 2; i++) {
                    const pos = (idx - 1 - i + MAX_POINTS) % MAX_POINTS;
                    prevSumRms += rmsBuffer.current[pos];
                }
                const prevMeanRms = prevSumRms / windowSize;
                const trend = meanRms - prevMeanRms;
                
                const timeContext = getTimeContext();
                
                const newAssumptions = (!activeSessionRef.current || !hasData.current) ? [] : evaluateAssumptions({
                    meanRms,
                    trend,
                    variance
                });
                
                setStats({ meanRms, variance, peak: maxAmp, zcr });
                
                setAssumptions(prev => {
                    if (prev.length !== newAssumptions.length || !prev.every((val, index) => val === newAssumptions[index])) {
                        return newAssumptions;
                    }
                    return prev;
                });
            }
        };
        
        rafId = requestAnimationFrame(loop);
        return () => cancelAnimationFrame(rafId);
    }, []);

    return (
        <div className="live-acoustic-panel" style={{ 
            display: 'flex', 
            flexDirection: 'column', 
            gap: '10px',
            backgroundColor: 'rgba(20, 24, 34, 0.6)',
            borderRadius: '12px',
            padding: '16px',
            boxShadow: '0 4px 20px rgba(0, 0, 0, 0.2)'
        }}>
            <div className="assumptions-badges" style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', minHeight: '32px' }}>
                {assumptions.map((a, i) => (
                    <span key={i} style={{
                        background: 'rgba(255, 255, 255, 0.03)',
                        color: 'rgba(255, 255, 255, 0.5)',
                        padding: '3px 8px',
                        borderRadius: '4px',
                        fontSize: '0.75rem',
                        fontWeight: '500',
                        letterSpacing: '0.5px',
                        textTransform: 'uppercase'
                    }}>
                        {a}
                    </span>
                ))}
                {assumptions.length === 0 && (
                    <span style={{ color: '#7A8CA8', fontSize: '0.85rem', fontStyle: 'italic', padding: '4px 0' }}>
                        Waiting for audio signal...
                    </span>
                )}
            </div>
            
            <div className="chart-container" style={{ height: '220px', width: '100%', position: 'relative' }}>
                <LiveMetricsChart 
                    rmsBuffer={rmsBuffer} 
                    ampBuffer={ampBuffer} 
                    zcrBuffer={zcrBuffer}
                    bufferIndex={bufferIndex}
                    maxPoints={MAX_POINTS}
                />
            </div>
            
            <div className="small-stats" style={{ display: 'flex', gap: '16px', color: '#7A8CA8', fontSize: '0.85rem', justifyContent: 'space-between', marginTop: '4px' }}>
                <div style={{ display: 'flex', gap: '16px' }}>
                    <span>Variance: <strong style={{ color: '#CFEFFF' }}>{stats.variance.toFixed(3)}</strong></span>
                </div>
                <div style={{ display: 'flex', gap: '12px' }}>
                    <span style={{ color: 'rgba(110, 193, 255, 0.9)' }}>— RMS</span>
                    <span style={{ color: 'rgba(255, 120, 120, 0.7)' }}>— Amp</span>
                    <span style={{ color: 'rgba(120, 255, 160, 0.5)' }}>— ZCR</span>
                </div>
            </div>

            <div className="metrics-panel" style={{ marginTop: '12px' }}>
                <div className="metric-card">
                    <div className="metric-label">MEAN RMS</div>
                    <div className="metric-value" style={{ color: '#6EC1FF' }}>
                        {stats.meanRms.toFixed(2)}
                    </div>
                </div>

                <div className="metric-card">
                    <div className="metric-label">PEAK AMP</div>
                    <div className="metric-value" style={{ color: '#6EC1FF' }}>
                        {stats.peak.toFixed(2)}
                    </div>
                </div>

                <div className="metric-card">
                    <div className="metric-label">MEAN ZCR</div>
                    <div className="metric-value" style={{ color: '#6EC1FF' }}>
                        {stats.zcr.toFixed(3)}
                    </div>
                </div>
            </div>
        </div>
    );
};

export default LiveAcousticPanel;
