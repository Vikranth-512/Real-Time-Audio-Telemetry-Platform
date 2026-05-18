/**
 * acousticAnalytics.js
 * Pure, stateless analytics functions. No React. No side effects.
 * All inputs are arrays of { timestamp, rms, peak, zcr, frequency }.
 * All functions run in O(N) or O(N log N) at worst.
 */

const SILENCE_THRESHOLD = 0.008;
const SPIKE_TRIGGER_SIGMA = 2.5;
const SPIKE_RELEASE_SIGMA = 1.8;
const SPIKE_COOLDOWN_FRAMES = 3;

// ─── EMA ─────────────────────────────────────────────────────────────────────

export function computeEMA(values, alpha = 0.15) {
    if (!values || values.length === 0) return [];
    const out = new Array(values.length);
    out[0] = values[0];
    for (let i = 1; i < values.length; i++) {
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1];
    }
    return out;
}

// ─── Rolling stats (O(N), running sums) ──────────────────────────────────────

export function computeRollingStats(values, windowSize) {
    const n = values.length;
    const means = new Float32Array(n);
    const variances = new Float32Array(n);
    let sum = 0, sumSq = 0;

    for (let i = 0; i < n; i++) {
        const v = values[i];
        sum += v;
        sumSq += v * v;

        if (i >= windowSize) {
            const old = values[i - windowSize];
            sum -= old;
            sumSq -= old * old;
        }

        const count = Math.min(i + 1, windowSize);
        const mean = sum / count;
        means[i] = mean;
        variances[i] = Math.max(0, sumSq / count - mean * mean);
    }
    return { means, variances };
}

// ─── LTTB Downsampling ────────────────────────────────────────────────────────

export function lttbDownsample(points, threshold) {
    const len = points.length;
    if (len <= threshold || threshold < 3) return points;

    const sampled = [points[0]];
    let a = 0;
    const bucketSize = (len - 2) / (threshold - 2);

    for (let i = 0; i < threshold - 2; i++) {
        const rangeStart = Math.floor((i + 1) * bucketSize) + 1;
        const rangeEnd = Math.min(Math.floor((i + 2) * bucketSize) + 1, len);
        const nextStart = Math.min(Math.floor((i + 2) * bucketSize) + 1, len - 1);
        const nextEnd = Math.min(Math.floor((i + 3) * bucketSize) + 1, len);

        let avgX = 0, avgY = 0, avgCount = 0;
        for (let j = nextStart; j < nextEnd; j++) {
            avgX += points[j].x;
            avgY += points[j].y;
            avgCount++;
        }
        if (avgCount > 0) { avgX /= avgCount; avgY /= avgCount; }

        let maxArea = -1, maxIdx = rangeStart;
        const ax = points[a].x, ay = points[a].y;
        for (let j = rangeStart; j < rangeEnd; j++) {
            const area = Math.abs((ax - avgX) * (points[j].y - ay) - (ax - points[j].x) * (avgY - ay)) * 0.5;
            if (area > maxArea) { maxArea = area; maxIdx = j; }
        }

        sampled.push(points[maxIdx]);
        a = maxIdx;
    }

    sampled.push(points[len - 1]);
    return sampled;
}

// ─── Spike detection with hysteresis ─────────────────────────────────────────

export function detectSpikes(metrics) {
    if (!metrics || metrics.length < 10) {
        return { spikes: [], sustainedRegions: [], burstDensity: 0 };
    }

    const rmsArr = metrics.map(m => m.rms || 0);
    const n = rmsArr.length;

    // Global mean + std in one pass
    let sum = 0, sumSq = 0;
    for (let i = 0; i < n; i++) { sum += rmsArr[i]; sumSq += rmsArr[i] * rmsArr[i]; }
    const mean = sum / n;
    const std = Math.sqrt(Math.max(0, sumSq / n - mean * mean));

    const triggerThresh = mean + SPIKE_TRIGGER_SIGMA * std;
    const releaseThresh = mean + SPIKE_RELEASE_SIGMA * std;

    const spikes = [];
    const spikeIndexSet = new Set();
    let cooldown = 0;
    let inSpike = false;

    for (let i = 0; i < n; i++) {
        if (cooldown > 0) { cooldown--; continue; }

        const v = rmsArr[i];
        if (!inSpike && v >= triggerThresh) {
            const severity = v > mean + 3.5 * std ? 'high' : 'medium';
            spikes.push({ index: i, timestamp: metrics[i].timestamp, value: v, severity });
            spikeIndexSet.add(i);
            inSpike = true;
            cooldown = SPIKE_COOLDOWN_FRAMES;
        } else if (inSpike && v < releaseThresh) {
            inSpike = false;
        }
    }

    // Cluster consecutive spikes (gap < 5 frames) into sustained regions
    const sustainedRegions = [];
    let regionStart = null;
    for (let i = 0; i < spikes.length; i++) {
        if (regionStart === null) regionStart = spikes[i];
        const next = spikes[i + 1];
        if (!next || next.index - spikes[i].index > 5) {
            if (i > 0 && spikes[i].index !== regionStart.index) {
                sustainedRegions.push({ start: regionStart.timestamp, end: spikes[i].timestamp });
            }
            regionStart = null;
        }
    }

    const dur = n > 1 ? (metrics[n - 1].timestamp - metrics[0].timestamp) : 1;
    const burstDensity = spikes.length / Math.max(dur, 1);

    return { spikes, sustainedRegions, burstDensity, spikeIndexSet };
}

// ─── Quiet regions ────────────────────────────────────────────────────────────

export function detectQuietRegions(metrics) {
    const regions = [];
    let start = null;
    let quietCount = 0;

    for (let i = 0; i < metrics.length; i++) {
        const isQuiet = (metrics[i].rms || 0) <= SILENCE_THRESHOLD;
        if (isQuiet) {
            if (start === null) start = metrics[i].timestamp;
            quietCount++;
        } else if (start !== null) {
            regions.push({
                start,
                end: metrics[i - 1].timestamp,
                duration: metrics[i - 1].timestamp - start
            });
            start = null;
        }
    }

    if (start !== null) {
        const last = metrics[metrics.length - 1];
        regions.push({ start, end: last.timestamp, duration: last.timestamp - start });
    }

    const idlePercent = metrics.length > 0 ? Math.round((quietCount / metrics.length) * 100) : 0;
    return { regions, idlePercent };
}

// ─── Frequency drift (linear regression, O(N)) ───────────────────────────────

export function computeFrequencyDrift(metrics) {
    const freqs = metrics.map(m => m.frequency || 0).filter(f => f > 20);
    const n = freqs.length;

    if (n < 5) return { driftSlope: 0, driftVariance: 0, stabilityLabel: 'insufficient data', mean: 0 };

    let sumX = 0, sumY = 0, sumXY = 0, sumX2 = 0;
    for (let i = 0; i < n; i++) {
        sumX += i; sumY += freqs[i]; sumXY += i * freqs[i]; sumX2 += i * i;
    }
    const denom = n * sumX2 - sumX * sumX;
    const driftSlope = denom !== 0 ? (n * sumXY - sumX * sumY) / denom : 0;
    const mean = sumY / n;

    let sumSq = 0;
    for (let i = 0; i < n; i++) sumSq += (freqs[i] - mean) ** 2;
    const driftVariance = sumSq / n;

    const stabilityLabel = driftVariance < 200 ? 'stable' : driftVariance < 3000 ? 'drifting' : 'unstable';

    return { driftSlope, driftVariance, stabilityLabel, mean };
}

// ─── Tonality ─────────────────────────────────────────────────────────────────

export function classifyTonality(metrics) {
    const n = metrics.length;
    if (n === 0) return 'unknown';

    let sumZcr = 0;
    const freqs = [];

    for (const m of metrics) {
        sumZcr += (m.zcr || 0);
        if (m.frequency > 20) freqs.push(m.frequency);
    }

    const meanZcr = sumZcr / n;
    const nf = freqs.length;
    let freqMean = 0;
    for (const f of freqs) freqMean += f;
    freqMean /= nf || 1;

    let freqSumSq = 0;
    for (const f of freqs) freqSumSq += (f - freqMean) ** 2;
    const freqStd = Math.sqrt(freqSumSq / (nf || 1));

    if (meanZcr < 0.05 && freqStd < 80) return 'tonal';
    if (meanZcr > 0.22 || freqStd > 400) return 'noisy';
    return 'mixed';
}

// ─── Session segmentation ─────────────────────────────────────────────────────

function classifyWindow(meanRms, variance, spikeDensity) {
    if (meanRms <= SILENCE_THRESHOLD) return 'quiet';
    if (spikeDensity > 0.4) return 'burst-heavy';
    if (variance > 0.025) return 'chaotic';
    if (meanRms > 0.25) return 'active';
    return 'stable';
}

export function segmentSession(metrics, spikeIndexSet = new Set()) {
    const WINDOW = 12;
    const STEP = 6;
    const MIN_DURATION_FRAMES = 4;

    if (metrics.length < WINDOW) return [];

    const raw = [];
    for (let i = 0; i + WINDOW <= metrics.length; i += STEP) {
        let sumRms = 0, sumSq = 0, spikeCount = 0;
        for (let j = i; j < i + WINDOW; j++) {
            const v = metrics[j].rms || 0;
            sumRms += v;
            sumSq += v * v;
            if (spikeIndexSet.has(j)) spikeCount++;
        }
        const mean = sumRms / WINDOW;
        const variance = Math.max(0, sumSq / WINDOW - mean * mean);
        const spikeDensity = spikeCount / WINDOW;

        raw.push({
            start: metrics[i].timestamp,
            end: metrics[Math.min(i + WINDOW - 1, metrics.length - 1)].timestamp,
            type: classifyWindow(mean, variance, spikeDensity),
            score: Math.round(Math.min(mean * 200, 100)),
            _frames: WINDOW,
        });
    }

    // Merge consecutive same-type segments
    const merged = [];
    for (const seg of raw) {
        const last = merged[merged.length - 1];
        if (last && last.type === seg.type) {
            last.end = seg.end;
            last.score = Math.round((last.score + seg.score) / 2);
            last._frames += seg._frames;
        } else {
            merged.push({ ...seg });
        }
    }

    // Drop tiny fragments
    return merged.filter(seg => seg._frames >= MIN_DURATION_FRAMES);
}

// ─── Scores ───────────────────────────────────────────────────────────────────

export function computeActivityScore(metrics, spikeCount) {
    if (!metrics.length) return 0;
    let sumRms = 0, sumSq = 0;
    const freqs = [];

    for (const m of metrics) {
        sumRms += m.rms || 0;
        sumSq += (m.rms || 0) ** 2;
        if (m.frequency > 20) freqs.push(m.frequency);
    }

    const n = metrics.length;
    const meanRms = sumRms / n;
    const variance = Math.max(0, sumSq / n - meanRms * meanRms);

    let freqMovement = 0;
    for (let i = 1; i < freqs.length; i++) freqMovement += Math.abs(freqs[i] - freqs[i - 1]);
    freqMovement = freqs.length > 1 ? freqMovement / (freqs.length - 1) : 0;

    const dur = n > 1 ? metrics[n - 1].timestamp - metrics[0].timestamp : 1;
    const burstDensity = spikeCount / Math.max(dur, 1);

    const score =
        Math.min(meanRms / 0.35, 1) * 40 +
        Math.min(variance / 0.02, 1) * 30 +
        Math.min(burstDensity / 2, 1) * 20 +
        Math.min(freqMovement / 80, 1) * 10;

    return Math.round(Math.min(score, 100));
}

export function computeStabilityScore(metrics) {
    if (!metrics.length) return 100;
    let sumRms = 0, sumSqRms = 0, sumZcr = 0, sumSqZcr = 0;

    for (const m of metrics) {
        sumRms += m.rms || 0;
        sumSqRms += (m.rms || 0) ** 2;
        sumZcr += m.zcr || 0;
        sumSqZcr += (m.zcr || 0) ** 2;
    }

    const n = metrics.length;
    const rmsVar = Math.max(0, sumSqRms / n - (sumRms / n) ** 2);
    const zcrVar = Math.max(0, sumSqZcr / n - (sumZcr / n) ** 2);
    const { driftVariance } = computeFrequencyDrift(metrics);

    const instability =
        Math.min(rmsVar / 0.04, 1) * 40 +
        Math.min(zcrVar / 0.008, 1) * 30 +
        Math.min(driftVariance / 4000, 1) * 30;

    return Math.round(Math.max(0, 100 - instability));
}

// ─── Assumptions ─────────────────────────────────────────────────────────────

export function generateAssumptions(analytics) {
    const { activityScore, stabilityScore, tonality, spikeResult, quietResult, freqDrift, segments } = analytics;
    const assumptions = [];

    const totalSegs = segments.length;
    const chaoticSegs = segments.filter(s => s.type === 'chaotic').length;
    const burstSegs = segments.filter(s => s.type === 'burst-heavy').length;

    if (spikeResult.burstDensity > 1.0) {
        assumptions.push({ severity: 'high', confidence: Math.min(spikeResult.burstDensity / 3, 0.99).toFixed(2), label: 'High Spike Density', description: 'Frequent transient acoustic spikes detected throughout the session.' });
    } else if (spikeResult.spikes.length > 4) {
        assumptions.push({ severity: 'medium', confidence: '0.76', label: 'Moderate Spikes', description: 'Intermittent acoustic spikes observed. Possible impact events or sudden sounds.' });
    }

    if (tonality === 'tonal') {
        assumptions.push({ severity: 'info', confidence: '0.83', label: 'Tonal Profile', description: 'Predominantly tonal acoustic profile. Consistent harmonic source detected.' });
    } else if (tonality === 'noisy') {
        assumptions.push({ severity: 'medium', confidence: '0.79', label: 'Noisy Environment', description: 'High zero-crossing rate indicates broadband noise or chaotic acoustic environment.' });
    } else if (tonality === 'mixed') {
        assumptions.push({ severity: 'info', confidence: '0.65', label: 'Mixed Signal', description: 'Mixed tonal and noise characteristics detected across session.' });
    }

    if (stabilityScore >= 75) {
        assumptions.push({ severity: 'info', confidence: '0.91', label: 'Stable Session', description: 'Consistent acoustic environment with minimal variance throughout.' });
    } else if (stabilityScore < 35) {
        assumptions.push({ severity: 'high', confidence: '0.88', label: 'Unstable Environment', description: 'Chaotic acoustic behavior observed. High variance detected across the session.' });
    }

    if (activityScore >= 70) {
        assumptions.push({ severity: 'medium', confidence: '0.86', label: 'High Activity Level', description: 'High acoustic energy and frequent transients indicate an active environment.' });
    } else if (activityScore < 20) {
        assumptions.push({ severity: 'info', confidence: '0.90', label: 'Low Activity Level', description: 'Session was predominantly low-energy with minimal acoustic activity.' });
    }

    if (freqDrift.stabilityLabel === 'unstable') {
        assumptions.push({ severity: 'high', confidence: '0.81', label: 'Significant Frequency Drift', description: 'Wide frequency variance detected. Source may be mobile or rapidly changing.' });
    } else if (freqDrift.driftSlope > 0.5) {
        assumptions.push({ severity: 'medium', confidence: '0.72', label: 'Rising Frequency Trend', description: 'Environment became progressively noisier toward end of session.' });
    } else if (freqDrift.driftSlope < -0.5) {
        assumptions.push({ severity: 'low', confidence: '0.70', label: 'Falling Frequency Trend', description: 'Signal source quieted progressively over the session duration.' });
    }

    if (quietResult.idlePercent >= 40) {
        assumptions.push({ severity: 'low', confidence: '0.93', label: 'Extended Quiet Periods', description: `${quietResult.idlePercent}% of session was near-silence. Likely intermittent acoustic source.` });
    }

    if (totalSegs > 0 && chaoticSegs > 0) {
        const pct = Math.round((chaoticSegs / totalSegs) * 100);
        assumptions.push({ severity: pct > 40 ? 'high' : 'medium', confidence: (0.60 + pct / 250).toFixed(2), label: 'Chaotic Segments Detected', description: `${pct}% of session segments were acoustically chaotic.` });
    }

    if (totalSegs > 0 && burstSegs > 0) {
        assumptions.push({ severity: 'medium', confidence: '0.77', label: 'Burst Cluster Activity', description: 'High burst density during portions of session. Possible repetitive impact source.' });
    }

    if (assumptions.length === 0) {
        assumptions.push({ severity: 'info', confidence: '1.00', label: 'Quiet Recording', description: 'Session was predominantly silent or very low energy throughout.' });
    }

    return assumptions;
}

// ─── Session fingerprint ──────────────────────────────────────────────────────

export function generateFingerprint(analytics) {
    const { activityScore, stabilityScore, tonality, spikeResult, freqDrift, quietResult } = analytics;
    return {
        dominantPattern: activityScore > 60 ? 'active' : quietResult.idlePercent > 40 ? 'intermittent' : 'steady',
        tonality,
        stabilityClass: stabilityScore >= 70 ? 'stable' : stabilityScore >= 40 ? 'variable' : 'chaotic',
        activityClass: activityScore >= 70 ? 'high' : activityScore >= 40 ? 'moderate' : 'low',
        spikeProfile: spikeResult.burstDensity > 1 ? 'spike-heavy' : spikeResult.spikes.length > 0 ? 'occasional-spikes' : 'clean',
        driftProfile: freqDrift.stabilityLabel,
    };
}

// ─── Run full analytics pipeline ─────────────────────────────────────────────

export function runAnalytics(rawMetrics) {
    if (!rawMetrics || rawMetrics.length === 0) return null;

    const spikeResult = detectSpikes(rawMetrics);
    const quietResult = detectQuietRegions(rawMetrics);
    const freqDrift = computeFrequencyDrift(rawMetrics);
    const tonality = classifyTonality(rawMetrics);
    const segments = segmentSession(rawMetrics, spikeResult.spikeIndexSet);
    const activityScore = computeActivityScore(rawMetrics, spikeResult.spikes.length);
    const stabilityScore = computeStabilityScore(rawMetrics);

    const analytics = {
        spikeResult,
        quietResult,
        freqDrift,
        tonality,
        segments,
        activityScore,
        stabilityScore,
    };

    analytics.assumptions = generateAssumptions(analytics);
    analytics.fingerprint = generateFingerprint(analytics);

    // Pre-compute EMA for display
    analytics.rmsEma = computeEMA(rawMetrics.map(m => m.rms || 0), 0.12);
    analytics.freqEma = computeEMA(rawMetrics.map(m => m.frequency || 0).filter(f => f > 0), 0.10);

    return analytics;
}
