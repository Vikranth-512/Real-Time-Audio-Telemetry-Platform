import React, { useEffect, useRef } from 'react';

const LiveMetricsChart = ({ rmsBuffer, ampBuffer, zcrBuffer, bufferIndex, maxPoints }) => {
    const canvasRef = useRef(null);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;
        
        const ctx = canvas.getContext('2d');
        let rafId;

        const drawChart = () => {
            rafId = requestAnimationFrame(drawChart);

            const dpr = window.devicePixelRatio || 1;
            const width = canvas.clientWidth * dpr;
            const height = canvas.clientHeight * dpr;

            if (canvas.width !== width || canvas.height !== height) {
                canvas.width = width;
                canvas.height = height;
            }

            ctx.clearRect(0, 0, width, height);

            const idx = bufferIndex.current;
            const stepX = width / maxPoints;

            // Define max values for scaling
            const MAX_RMS = 1.0;
            const MAX_AMP = 1.5;
            const MAX_ZCR = 1.0;

            const renderLine = (buffer, maxValue, color, lineWidth, fillGlow = false) => {
                ctx.beginPath();
                ctx.strokeStyle = color;
                ctx.lineWidth = lineWidth;
                
                let firstValid = false;

                for (let i = 0; i < maxPoints; i++) {
                    const dataIdx = (idx + i) % maxPoints;
                    const val = buffer.current[dataIdx];
                    
                    // Normalize to 0-1
                    const normalized = Math.min(1, Math.max(0, val / maxValue));
                    
                    const x = i * stepX;
                    const y = height - (normalized * height);

                    if (i === 0) {
                        ctx.moveTo(x, y);
                    } else {
                        ctx.lineTo(x, y);
                    }
                    firstValid = true;
                }

                if (firstValid) {
                    ctx.stroke();

                    if (fillGlow) {
                        ctx.lineTo(width, height);
                        ctx.lineTo(0, height);
                        ctx.closePath();
                        
                        const gradient = ctx.createLinearGradient(0, 0, 0, height);
                        gradient.addColorStop(0, color.replace(/[\d.]+\)$/g, '0.2)')); // Extract RGB and set alpha
                        gradient.addColorStop(1, color.replace(/[\d.]+\)$/g, '0.0)'));
                        
                        ctx.fillStyle = gradient;
                        ctx.fill();
                    }
                }
            };

            // Draw ZCR (Background/Overlay)
            renderLine(zcrBuffer, MAX_ZCR, 'rgba(120, 255, 160, 0.5)', 1.5);
            
            // Draw Amplitude (Secondary)
            renderLine(ampBuffer, MAX_AMP, 'rgba(255, 120, 120, 0.7)', 1.5);

            // Draw RMS (Primary)
            renderLine(rmsBuffer, MAX_RMS, 'rgba(110, 193, 255, 0.9)', 2.5, true);
        };

        rafId = requestAnimationFrame(drawChart);

        return () => {
            cancelAnimationFrame(rafId);
        };
    }, [rmsBuffer, ampBuffer, zcrBuffer, bufferIndex, maxPoints]);

    return (
        <canvas 
            ref={canvasRef} 
            style={{ 
                width: '100%', 
                height: '100%', 
                display: 'block',
                position: 'absolute',
                inset: 0
            }} 
        />
    );
};

export default LiveMetricsChart;
