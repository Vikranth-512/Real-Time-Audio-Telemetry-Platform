import numpy as np
from typing import List, Dict, Any

# ── Configuration Constants ─────────────────────────────────────────────────
MAD_SCALE = 1.4826          # scale factor for MAD → σ equivalence
MIN_SEGMENT_DURATION = 3.0  # seconds — merge shorter segments

# States
UNKNOWN = -1
QUIET = 0
STEADY = 1
ACTIVE = 2
BURST = 3

STATE_NAMES = {
    UNKNOWN: "unknown",
    QUIET: "quiet",
    STEADY: "steady",
    ACTIVE: "active",
    BURST: "burst"
}

# Z-Score thresholds for base states
Z_QUIET = -1.5
Z_ACTIVE = 1.5

# Anomaly score thresholds
ANOMALY_MINOR = 3.0
ANOMALY_MAJOR = 5.0
ANOMALY_CRITICAL = 7.0

# Event duration thresholds (seconds)
SUSTAINED_ACTIVITY_MIN = 15.0

# Transition Merge Cost Matrix
MERGE_COST = {
    (BURST, ACTIVE): 1, (ACTIVE, BURST): 1,
    (ACTIVE, STEADY): 3, (STEADY, ACTIVE): 3,
    (STEADY, QUIET): 2, (QUIET, STEADY): 2,
    (QUIET, ACTIVE): 10, (ACTIVE, QUIET): 10,
    (QUIET, BURST): 100, (BURST, QUIET): 100,
    (STEADY, BURST): 50, (BURST, STEADY): 50,
}
DEFAULT_MERGE_COST = 10


def _rolling_percentile(arr: np.ndarray, window: int, percentile: float = 50.0) -> np.ndarray:
    """Vectorized rolling percentile using numpy sliding_window_view."""
    n = len(arr)
    if n == 0:
        return arr
    if window < 1: window = 1
    if window >= n: window = n
    
    pad_left = window // 2
    pad_right = window - 1 - pad_left
    padded = np.pad(arr, (pad_left, pad_right), mode='edge')
    
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(padded, window_shape=window)
    return np.nanpercentile(windows, percentile, axis=1)

def _rolling_median(arr: np.ndarray, window: int) -> np.ndarray:
    return _rolling_percentile(arr, window, 50.0)

def _fill_nans(arr: np.ndarray) -> np.ndarray:
    """Forward/backward fill NaNs in a 1D array."""
    mask = np.isnan(arr)
    if not np.any(mask):
        return arr
    idx = np.where(~mask, np.arange(mask.shape[0]), 0)
    np.maximum.accumulate(idx, out=idx)
    arr = arr[idx]
    mask = np.isnan(arr)
    if np.any(mask):
        idx = np.where(~mask, np.arange(mask.shape[0]), mask.shape[0]-1)
        idx = np.minimum.accumulate(idx[::-1])[::-1]
        arr = arr[idx]
    return arr


def generate_timeline(metrics: List[Dict]) -> Dict[str, Any]:
    """Generates a deterministic timeline analysis from a session's full_metrics."""
    if not metrics or len(metrics) < 2:
        return _empty_timeline()

    # ── 1. Extract & sort ───────────────────────────────────────────────────
    n_samples = len(metrics)
    ts_raw = np.empty(n_samples, dtype=np.float64)
    rms_raw = np.empty(n_samples, dtype=np.float64)
    peak_raw = np.empty(n_samples, dtype=np.float64)
    zcr_raw = np.empty(n_samples, dtype=np.float64)

    for i, m in enumerate(metrics):
        ts_raw[i] = m["timestamp"]
        md = m.get("metrics") or {}
        rms_raw[i] = float(md.get("rms") or 0.0)
        peak_raw[i] = float(md.get("peak") or 0.0)
        zcr_raw[i] = float(md.get("zcr") or 0.0) 

    order = np.argsort(ts_raw, kind="quicksort")
    timestamps = ts_raw[order]
    rms_raw = rms_raw[order]
    peak_raw = peak_raw[order]
    zcr_raw = zcr_raw[order]

    t_norm = timestamps - timestamps[0]
    duration = float(t_norm[-1])
    if duration <= 0:
        return _empty_timeline()

    # ── 2. Preprocessing — identify valid samples ───────────────────────────
    valid_mask = (rms_raw > 0) & (peak_raw > 0)
    valid_count = np.sum(valid_mask)
    if valid_count < 3:
        return _empty_timeline()
        
    rms_clean = rms_raw.copy()
    rms_clean[~valid_mask] = np.nan
    valid_rms = rms_raw[valid_mask]
    
    # Anchor global baseline to the quietest persistent floor (10th percentile), not median,
    # to prevent it from being artificially inflated by long speech or HVAC events.
    global_baseline = float(np.percentile(valid_rms, 10.0))
    
    # ── 2b. Timing ───────────────────────────────────────────────────────────
    dt = np.diff(timestamps)
    dt_median = np.median(dt) if len(dt) > 0 else 1.0
    samples_per_second = 1.0 / max(dt_median, 1e-6)

    # Coverage & Continuity (computed early for fast-path)
    expected_samples = max(1, duration * samples_per_second)
    coverage_score = min(1.0, valid_count / expected_samples)
    gaps = np.diff(t_norm)
    largest_gap = float(np.max(gaps)) if len(gaps) > 0 else 0.0
    continuity_score = max(0.0, 1.0 - (largest_gap / max(duration, 1.0)))

    # Ultra-stable fast-path (also checks ZCR and passes coverage through)
    if valid_count > 10:
        valid_peak = peak_raw[valid_mask]
        valid_zcr = zcr_raw[valid_mask]
        cv_rms = float(np.std(valid_rms) / max(np.mean(valid_rms), 1e-6))
        cv_peak = float(np.std(valid_peak) / max(np.mean(valid_peak), 1e-6))
        cv_zcr = float(np.std(valid_zcr) / max(np.mean(valid_zcr), 1e-6)) if np.mean(valid_zcr) > 1e-6 else 0.0
        
        if cv_rms < 0.005 and cv_peak < 0.005 and cv_zcr < 0.05:
            fp_confidence = round(coverage_score * continuity_score, 2)
            return _fast_path_stable_timeline(global_baseline, duration, metrics, fp_confidence)

    # ── 3. Dual-Timescale Baseline ──────────────────────────────────────────
    fast_window_seconds = np.clip(duration * 0.10, 15.0, 60.0)
    fast_window_samples = max(5, int(fast_window_seconds * samples_per_second))
    fast_baseline_rms = _fill_nans(_rolling_median(rms_clean, fast_window_samples))
    
    # Asymmetric EMA for slow baseline: prevents absorbing long speech blocks (Test 26)
    # while remaining anchored to the true ambient floor.
    slow_baseline_rms = np.zeros_like(rms_clean)
    if len(rms_clean) > 0:
        alpha_up = 1.0 / max(1.0, 900.0 * samples_per_second)
        alpha_down = 1.0 / max(1.0, 10.0 * samples_per_second)
        quiet_start = float(np.nanpercentile(rms_clean[:max(5, int(15.0 * samples_per_second))], 25.0))
        if np.isnan(quiet_start): quiet_start = global_baseline
        
        slow_baseline_rms[0] = quiet_start
        for i in range(1, len(rms_clean)):
            if np.isnan(rms_clean[i]):
                slow_baseline_rms[i] = slow_baseline_rms[i-1]
            elif rms_clean[i] > slow_baseline_rms[i-1]:
                slow_baseline_rms[i] = slow_baseline_rms[i-1] + alpha_up * (rms_clean[i] - slow_baseline_rms[i-1])
            else:
                slow_baseline_rms[i] = slow_baseline_rms[i-1] + alpha_down * (rms_clean[i] - slow_baseline_rms[i-1])
    slow_baseline_rms = _fill_nans(slow_baseline_rms)

    # Variance scaling
    residual_rms_fast = valid_rms - fast_baseline_rms[valid_mask]
    mad_rms_fast = float(np.median(np.abs(residual_rms_fast)))
    
    # Slow MAD: measure the baseline's own temporal variability, NOT
    # signal deviation from it (which would be inflated by activity).
    slow_baseline_center = float(np.median(slow_baseline_rms[valid_mask]))
    mad_rms_slow = float(np.median(np.abs(slow_baseline_rms[valid_mask] - slow_baseline_center)))
    
    # Global variance floor must be large enough to prevent false actives in quiet sessions
    min_variance_floor = max(2e-3, global_baseline * 0.05)
    scaled_mad_fast = max(MAD_SCALE * mad_rms_fast, min_variance_floor)
    scaled_mad_slow = max(MAD_SCALE * mad_rms_slow, min_variance_floor)

    # Gradient math for burst detection
    rms_grad = np.abs(np.diff(rms_clean, prepend=rms_clean[0]))
    valid_grad = rms_grad[valid_mask]
    grad_baseline = float(np.median(valid_grad))
    grad_mad = max(MAD_SCALE * float(np.median(np.abs(valid_grad - grad_baseline))), min_variance_floor)
    grad_z = (rms_grad - grad_baseline) / grad_mad

    # ── 4. Z-Scores and Hysteresis State Machine ────────────────────────────
    fast_z_scores = (rms_clean - fast_baseline_rms) / scaled_mad_fast
    slow_z_scores = (rms_clean - slow_baseline_rms) / scaled_mad_slow
    
    fast_z_clipped = np.clip(fast_z_scores[valid_mask], -10.0, 10.0)
    burst_enter = max(6.0, np.percentile(fast_z_clipped, 99.5))
    burst_exit = burst_enter * 0.75
    grad_enter = 3.0 # Threshold for temporal gradient entry
    
    classes = np.full(n_samples, UNKNOWN, dtype=np.int8)
    current_state = STEADY
    
    burst_start_time = None
    
    for i in range(n_samples):
        if not valid_mask[i]:
            classes[i] = UNKNOWN
            continue
            
        fz = fast_z_scores[i]
        sz = slow_z_scores[i]
        gz = grad_z[i]
        
        if current_state == BURST:
            # Remain burst based on fast baseline only (no gradient needed to stay)
            if fz < burst_exit:
                burst_start_time = None
                if sz > Z_ACTIVE: new_state = ACTIVE
                elif sz < Z_QUIET: new_state = QUIET
                else: new_state = STEADY
            else:
                new_state = BURST
        else:
            # Enter burst requires BOTH fast magnitude and high gradient
            if fz >= burst_enter and gz > grad_enter:
                new_state = BURST
                burst_start_time = ts_raw[i]
            else:
                burst_start_time = None
                if sz > Z_ACTIVE: new_state = ACTIVE
                elif sz < Z_QUIET: new_state = QUIET
                else: new_state = STEADY
            
        classes[i] = new_state
        current_state = new_state
        
    classes = _fill_nans(np.where(classes == UNKNOWN, np.nan, classes)).astype(np.int8)
    classes[classes == UNKNOWN] = STEADY

    # ── 5. Segment Generation & Transition Graph Merging ────────────────────
    changes = np.where(np.diff(classes) != 0)[0] + 1
    seg_starts = np.concatenate(([0], changes))
    seg_ends = np.concatenate((changes, [n_samples]))
    
    segs = []
    for s, e in zip(seg_starts, seg_ends):
        end_idx = min(e, n_samples - 1)
        t_s = float(t_norm[s])
        t_e = float(t_norm[end_idx]) if e < n_samples else float(duration)
        segs.append({
            "type_id": classes[s],
            "start": t_s,
            "end": t_e,
            "duration": t_e - t_s,
            "conf_modifier": 1.0 # Will be updated
        })
        
    def get_merge_cost(t1, t2):
        if t1 == t2: return 0
        return MERGE_COST.get((t1, t2), DEFAULT_MERGE_COST)
        
    while True:
        if len(segs) <= 1: break
        
        shortest_idx = -1
        shortest_dur = float('inf')
        for i, s in enumerate(segs):
            if s["type_id"] != BURST and s["duration"] < MIN_SEGMENT_DURATION and s["duration"] < shortest_dur:
                shortest_dur = s["duration"]
                shortest_idx = i
                
        if shortest_idx == -1: break
        
        short_seg = segs[shortest_idx]
        short_type = short_seg["type_id"]
        
        cost_left = float('inf')
        if shortest_idx > 0:
            cost_left = get_merge_cost(short_type, segs[shortest_idx-1]["type_id"])
            
        cost_right = float('inf')
        if shortest_idx < len(segs) - 1:
            cost_right = get_merge_cost(short_type, segs[shortest_idx+1]["type_id"])
            
        if cost_left == float('inf') and cost_right == float('inf'):
            segs.pop(shortest_idx)
            continue
            
        merge_idx = shortest_idx - 1 if cost_left <= cost_right else shortest_idx + 1
        
        target = segs[merge_idx]
        target["start"] = min(target["start"], short_seg["start"])
        target["end"] = max(target["end"], short_seg["end"])
        target["duration"] = target["end"] - target["start"]
        segs.pop(shortest_idx)
        
        out = []
        for s in segs:
            if out and out[-1]["type_id"] == s["type_id"]:
                out[-1]["end"] = s["end"]
                out[-1]["duration"] = out[-1]["end"] - out[-1]["start"]
            else:
                out.append(s)
        segs = out
        
    final_segments = []
    for s in segs:
        seg_conf = min(1.0, 0.70 + (s["duration"] / 10.0) * 0.30) * s["conf_modifier"]
        final_segments.append({
            "type": STATE_NAMES[s["type_id"]],
            "start": round(s["start"], 1),
            "end": round(s["end"], 1),
            "duration": round(s["duration"], 1),
            "confidence": round(seg_conf, 2)
        })

    # ── 6. Euclidean Anomaly Detection (Weighted) ───────────────────────────
    peak_clean = peak_raw.copy()
    peak_clean[~valid_mask] = np.nan
    local_peak_base = _rolling_median(peak_clean, fast_window_samples)
    residual_peak = peak_clean[valid_mask] - local_peak_base[valid_mask]
    peak_mad = float(np.median(np.abs(residual_peak)))
    global_peak = float(np.median(peak_clean[valid_mask]))
    peak_variance_floor = max(1e-4, global_peak * 0.01)
    peak_scaled_mad = max(MAD_SCALE * peak_mad, peak_variance_floor)
    peak_z = (peak_clean - local_peak_base) / peak_scaled_mad
    
    # Independent ZCR Normalization
    zcr_clean = zcr_raw.copy()
    zcr_clean[~valid_mask] = np.nan
    local_zcr_base = _rolling_median(zcr_clean, fast_window_samples)
    residual_zcr = zcr_clean[valid_mask] - local_zcr_base[valid_mask]
    zcr_mad = float(np.median(np.abs(residual_zcr)))
    global_zcr = float(np.median(zcr_clean[valid_mask]))
    # ZCR uses absolute floor + 10% relative, entirely decoupled from amplitude
    zcr_variance_floor = max(1e-5, global_zcr * 0.1)
    zcr_scaled_mad = max(MAD_SCALE * zcr_mad, zcr_variance_floor)
    zcr_z = (zcr_clean - local_zcr_base) / zcr_scaled_mad
    
    r_z = np.clip(np.nan_to_num(fast_z_scores, nan=0.0), -10.0, 10.0)
    p_z = np.clip(np.nan_to_num(peak_z, nan=0.0), -10.0, 10.0)
    f_z = np.clip(np.nan_to_num(zcr_z, nan=0.0), -10.0, 10.0)
    
    v_dims = (~np.isnan(fast_z_scores)).astype(int) + (~np.isnan(peak_z)).astype(int) + (~np.isnan(zcr_z)).astype(int)
    v_dims = np.maximum(v_dims, 1)
    
    # Weighted Euclidean Anomaly
    wr, wp, wz = 1.0, 1.0, 0.75
    anomaly_distance = np.sqrt((wr*r_z**2 + wp*p_z**2 + wz*f_z**2) / v_dims)
    
    candidates = []
    for i in range(1, n_samples - 1):
        if not valid_mask[i]: continue
        score = float(anomaly_distance[i])
        if score < ANOMALY_MINOR: continue
        # Relaxed peak check to allow flat-topped anomalies
        if anomaly_distance[i] < anomaly_distance[i-1] or anomaly_distance[i] < anomaly_distance[i+1]:
            continue
        candidates.append({"timestamp": float(t_norm[i]), "score": score})
        
    clusters = []
    curr_c = []
    for c in candidates:
        if not curr_c:
            curr_c.append(c)
        elif c["timestamp"] - curr_c[-1]["timestamp"] <= 5.0:
            curr_c.append(c)
        else:
            clusters.append(curr_c)
            curr_c = [c]
    if curr_c: clusters.append(curr_c)
    
    anomalies = []
    for cl in clusters:
        scores = [c["score"] for c in cl]
        m_score = max(scores)
        if m_score > ANOMALY_CRITICAL: sev = "critical"
        elif m_score > ANOMALY_MAJOR: sev = "major"
        else: sev = "minor"
        
        anomalies.append({
            "timestamp": round(cl[0]["timestamp"], 1),
            "end": round(cl[-1]["timestamp"], 1),
            "duration": round(cl[-1]["timestamp"] - cl[0]["timestamp"], 1),
            "score": round(m_score, 1),
            "avg_score": round(sum(scores)/len(scores), 1),
            "severity": sev
        })

    # ── 7. CUSUM Validation & Persistence ───────────────────────────────────
    events = []
    target_mean = 0.0 # Operates on slow baseline residuals
    cusum_pos = np.zeros(n_samples)
    cusum_neg = np.zeros(n_samples)
    drift = 0.5 * scaled_mad_slow
    cusum_thresh = max(5.0 * scaled_mad_slow, global_baseline * 0.05)
    cusum_warmup = max(fast_window_seconds, 30.0) # No CUSUM before baseline settles
    
    # Validation window = at least 2x the fast window (captures full oscillation cycle)
    validation_horizon = max(30.0, fast_window_seconds * 2.0)
    
    change_points = []
    last_cp_time = 0.0
    
    for i in range(1, n_samples):
        if not valid_mask[i]:
            cusum_pos[i] = cusum_pos[i-1]
            cusum_neg[i] = cusum_neg[i-1]
            continue
        
        # Skip warmup period
        if t_norm[i] < cusum_warmup:
            continue
            
        # Transition Density Check (cumulative and windowed fallback)
        elapsed = max(t_norm[i], 1.0)
        total_transitions = np.sum(np.diff(classes[0:i+1]) != 0)
        transition_rate = total_transitions / elapsed
        
        # Also check local transitions (last 30s) for aggressive oscillation suppression
        idx_30s = np.searchsorted(t_norm, t_norm[i] - 30.0)
        local_transitions = np.sum(np.diff(classes[max(0, idx_30s):i+1]) != 0)
        
        residual = rms_clean[i] - slow_baseline_rms[i]
        
        # Suppress CUSUM if oscillating globally or locally (>2 flips in 30s)
        if transition_rate > 0.033 or local_transitions >= 2:
            cusum_pos[i] = 0.0
            cusum_neg[i] = 0.0
            continue
            
        cusum_pos[i] = max(0.0, cusum_pos[i-1] + residual - target_mean - drift)
        cusum_neg[i] = max(0.0, cusum_neg[i-1] - residual + target_mean - drift)
        
        if (cusum_pos[i] > cusum_thresh or cusum_neg[i] > cusum_thresh) and (t_norm[i] - last_cp_time > 15.0):
            # Immediate Step Change Check (Reject Slow Drift - Test 8)
            imm_pre = np.where((t_norm >= t_norm[i] - 5.0) & (t_norm < t_norm[i]) & valid_mask)[0]
            imm_post = np.where((t_norm > t_norm[i]) & (t_norm <= t_norm[i] + 5.0) & valid_mask)[0]
            
            is_valid_candidate = True
            if len(imm_pre) > 1 and len(imm_post) > 1:
                step_diff = abs(np.mean(rms_clean[imm_post]) - np.mean(rms_clean[imm_pre]))
                if step_diff < max(1e-3, global_baseline * 0.15):
                    is_valid_candidate = False # Reject slow drift
            
            if is_valid_candidate:
                # Candidate Validation with wide pre/post windows
                pre_window = np.where((t_norm >= t_norm[i] - validation_horizon) & (t_norm < t_norm[i]) & valid_mask)[0]
                post_window = np.where((t_norm > t_norm[i]) & (t_norm <= t_norm[i] + validation_horizon) & valid_mask)[0]
                
                if len(pre_window) > 2 and len(post_window) > 2:
                    mean_pre = np.mean(rms_clean[pre_window])
                    mean_post = np.mean(rms_clean[post_window])
                    
                    # CV-based post-stability: the new environment must be stable in amplitude
                    std_post = np.std(rms_clean[post_window])
                    cv_post = std_post / max(mean_post, 1e-6)
                    
                    # ZCR Stability: True environments (HVAC) have stable ZCR. Speech has chaotic ZCR.
                    zcr_post = zcr_clean[post_window]
                    cv_zcr_post = np.std(zcr_post) / max(np.mean(zcr_post), 1e-6)
                    
                    # Require significant shift, low amplitude variance, and stable ZCR
                    # Also, reject shifts that are just the signal returning to the global quiet floor
                    if abs(mean_post - mean_pre) > max(1e-3, global_baseline * 0.15) and cv_post < 0.15 and cv_zcr_post < 0.30:
                        if mean_post > global_baseline * 1.5:
                            change_points.append(t_norm[i])
                            last_cp_time = t_norm[i]
                        
            cusum_pos[i] = 0.0
            cusum_neg[i] = 0.0
            
    for cp in change_points:
        events.append({
            "type": "environment_change",
            "timestamp": round(cp, 1),
            "duration": 0.0,
            "description": "Environment Shift",
        })

    # Persistence Layer for Segments
    for seg in final_segments:
        sd = seg["duration"]
        st = seg["type"]
        if st in ("active", "burst") and sd >= SUSTAINED_ACTIVITY_MIN:
            events.append({
                "type": "sustained_activity",
                "timestamp": seg["start"],
                "duration": sd,
                "description": f"Sustained Activity ({formatDur(sd)})",
            })
        if st == "burst" and sd < SUSTAINED_ACTIVITY_MIN:
            events.append({
                "type": "burst",
                "timestamp": seg["start"],
                "duration": sd,
                "description": f"Burst Event ({formatDur(sd)})",
            })
    
    events.sort(key=lambda e: e["timestamp"])

    # ── 8. Distribution & Metrics ───────────────────────────────────────────
    dist = {"quiet": 0.0, "steady": 0.0, "active": 0.0, "burst": 0.0}
    for seg in final_segments:
        dist[seg["type"]] = dist.get(seg["type"], 0.0) + seg["duration"]
        
    quiet_pct = dist["quiet"] / max(duration, 1e-6)
    steady_pct = dist["steady"] / max(duration, 1e-6)
    active_pct = dist["active"] / max(duration, 1e-6)
    burst_pct = dist["burst"] / max(duration, 1e-6)
    
    distribution = {
        "quiet_pct": round(quiet_pct * 100, 1),
        "steady_pct": round(steady_pct * 100, 1),
        "active_pct": round(active_pct * 100, 1),
        "burst_pct": round(burst_pct * 100, 1),
    }

    intensity_index = round((0*quiet_pct + 1*steady_pct + 2*active_pct + 4*burst_pct) / 4.0 * 100.0, 1)

    # Confidence is multiplicative, so a 70% coverage drops the final score properly
    timeline_confidence = round(coverage_score * continuity_score, 2)

    # Trend derived from slow_baseline
    s_base = slow_baseline_rms[0] if len(slow_baseline_rms) > 0 else global_baseline
    e_base = slow_baseline_rms[-1] if len(slow_baseline_rms) > 0 else global_baseline
    baseline_drift_pct = round((e_base - s_base) / max(s_base, 1e-6) * 100.0, 1)

    trend = "Stable"
    if n_samples >= 10:
        step = max(1, n_samples // 50)
        x_samp = t_norm[::step]
        y_samp = slow_baseline_rms[::step]
        slopes = []
        half = len(x_samp) // 2
        for i in range(half):
            dx = x_samp[i+half] - x_samp[i]
            if dx > 0:
                slopes.append((y_samp[i+half] - y_samp[i]) / dx)
        if slopes:
            expected_change = np.median(slopes) * duration
            rel_change = expected_change / max(global_baseline, 1e-6)
            if rel_change > 0.15: trend = "Increasing"
            elif rel_change < -0.15: trend = "Decreasing"

    # Environment State Classification
    transition_count = len(final_segments)
    transition_density = transition_count / max(duration, 1.0)
    
    if transition_density > 0.03: # 1 transition every ~30s
        env_state = "Highly Volatile"
    elif len(change_points) > 0:
        env_state = "Changing Environment"
    elif burst_pct > 0.20:
        env_state = "Burst Dominated"
    elif active_pct > 0.20:
        env_state = "Dynamic Ambient"
    else:
        env_state = "Stable Ambient"

    session_dynamics = env_state
    if burst_pct > 0.20 and intensity_index > 50:
        session_dynamics = "Highly Dynamic"
    elif len(change_points) > 1:
        session_dynamics = "Shifting Environments"
    elif active_pct > 0.20 or burst_pct > 0.05:
        session_dynamics = "Active Events Detected"
    elif quiet_pct + steady_pct > 0.90:
        session_dynamics = "Stable Ambient"
        
    session_fingerprint = f"{session_dynamics} (Intensity: {intensity_index}/100)"

    insights = {
        "longest_stable_period": max([s["duration"] for s in final_segments if s["type"] == "steady"], default=0.0),
        "activity_volatility": "High" if transition_density > 0.05 else "Medium" if transition_density > 0.02 else "Low",
        "anomaly_count": len(anomalies),
        "session_dynamics": session_dynamics,
        "session_fingerprint": session_fingerprint,
        "trend": trend,
        "timeline_confidence": timeline_confidence,
        "activity_intensity_index": intensity_index,
        "baseline_drift_pct": baseline_drift_pct,
        "environment_state": env_state,
        "missing_sample_ratio": round(1.0 - coverage_score, 3)
    }

    # ── 9. Fact-Driven Narrative Summary ────────────────────────────────────
    baseline_time = quiet_pct + steady_pct
    s_lines = []
    
    if len(change_points) > 0:
        s_lines.append(f"Acoustic environment shifted at {formatDur(change_points[0])}.")
    
    s_lines.append(f"The session remained within its baseline range for {round(baseline_time * 100)}% of its duration.")
    
    act_segs = [s for s in final_segments if s["type"] in ("active", "burst")]
    if act_segs:
        longest = max(act_segs, key=lambda x: x["duration"])
        if longest["duration"] > 5.0:
            s_lines.append(f"A major elevated activity period occurred at {formatDur(longest['start'])} and lasted {formatDur(longest['duration'])}.")
    
    if len(anomalies) > 0:
        s_lines.append(f"{len(anomalies)} structural anomalies were detected.")
    
    if abs(baseline_drift_pct) > 10.0:
        drift_dir = "louder" if baseline_drift_pct > 0 else "quieter"
        s_lines.append(f"The ambient environment grew {abs(baseline_drift_pct)}% {drift_dir} over the session.")
        
    summary = " ".join(s_lines)

    # Phases
    PHASE_MAP = {
        "steady": "Steady State",
        "active": "Sustained Activity",
        "burst": "Burst Cluster",
        "quiet": "Quiet Period",
    }
    phases = []
    for seg in final_segments:
        pn = PHASE_MAP.get(seg["type"], "Steady State")
        if phases and phases[-1]["name"] == pn:
            phases[-1]["end"] = seg["end"]
            phases[-1]["duration"] = round(phases[-1]["end"] - phases[-1]["start"], 1)
        else:
            phases.append({
                "name": pn,
                "start": seg["start"],
                "end": seg["end"],
                "duration": seg["duration"],
            })

    return {
        "baseline": {
            "rms": round(global_baseline, 4),
            "variability": round(mad_rms_slow, 4),
        },
        "duration": round(duration, 1),
        "distribution": distribution,
        "segments": final_segments,
        "events": events,
        "anomalies": anomalies,
        "phases": phases,
        "insights": insights,
        "summary": summary,
    }

def formatDur(seconds: float) -> str:
    """Human-readable duration for event descriptions."""
    if seconds < 60:
        return f"{round(seconds)}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s" if s else f"{m}m"

def _empty_timeline() -> Dict[str, Any]:
    return {
        "baseline": {"rms": 0.0, "variability": 0.0},
        "duration": 0.0,
        "distribution": {"quiet_pct": 0, "steady_pct": 0, "active_pct": 0, "burst_pct": 0},
        "segments": [],
        "events": [],
        "anomalies": [],
        "phases": [],
        "insights": {
            "longest_stable_period": 0,
            "activity_volatility": "Low",
            "anomaly_count": 0,
            "session_dynamics": "Mostly Stable",
            "session_fingerprint": "Empty Session",
            "trend": "Stable",
            "timeline_confidence": 0.0,
            "activity_intensity_index": 0.0,
            "baseline_drift_pct": 0.0,
            "environment_state": "Unknown",
            "missing_sample_ratio": 1.0
        },
        "summary": "Session was too short to generate a meaningful timeline.",
    }

def _fast_path_stable_timeline(baseline_rms: float, duration: float, metrics: List[Dict], confidence: float = 1.0) -> Dict[str, Any]:
    """Generates a perfect 'STEADY' timeline for mathematically ultra-stable sessions."""
    seg = {
        "type": "steady",
        "start": 0.0,
        "end": round(duration, 1),
        "duration": round(duration, 1),
        "confidence": confidence
    }
    
    return {
        "baseline": {
            "rms": round(baseline_rms, 4),
            "variability": 0.0001,
        },
        "duration": round(duration, 1),
        "distribution": {"quiet_pct": 0.0, "steady_pct": 100.0, "active_pct": 0.0, "burst_pct": 0.0},
        "segments": [seg],
        "events": [],
        "anomalies": [],
        "phases": [{
            "name": "Steady State",
            "start": 0.0,
            "end": round(duration, 1),
            "duration": round(duration, 1)
        }],
        "insights": {
            "longest_stable_period": round(duration, 1),
            "activity_volatility": "Low",
            "anomaly_count": 0,
            "session_dynamics": "Stable Ambient",
            "session_fingerprint": "Ultra-Stable Calibration",
            "trend": "Stable",
            "timeline_confidence": confidence,
            "activity_intensity_index": 25.0,
            "baseline_drift_pct": 0.0,
            "environment_state": "Stable Ambient",
            "missing_sample_ratio": round(1.0 - confidence, 3)
        },
        "summary": "The session remained mathematically stable for 100% of its duration, indicating a highly calibrated or pure ambient acoustic environment.",
    }
