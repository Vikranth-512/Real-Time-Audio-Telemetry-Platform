import numpy as np
import json
import traceback
from backend.processing.timeline_analyzer import generate_timeline

def make_metrics(timestamps, rms_vals, peak_vals, zcr_vals):
    return [{"timestamp": float(t), "metrics": {"rms": float(r), "peak": float(p), "zcr": float(z)}} for t, r, p, z in zip(timestamps, rms_vals, peak_vals, zcr_vals)]

def evaluate_test(name, desc, generator_fn, expected_fn):
    try:
        metrics = generator_fn()
        tl = generate_timeline(metrics)
        passed, reason = expected_fn(tl)
        return {"name": name, "desc": desc, "passed": passed, "reason": reason, "tl": tl}
    except Exception as e:
        return {"name": name, "desc": desc, "passed": False, "reason": f"Exception: {str(e)}", "tl": None}

tests = []

# TEST 1 — Perfect Stable Sine Wave
def gen_1():
    t = np.arange(0, 120.0, 1.0)
    return make_metrics(t, np.full(len(t), 0.12), np.full(len(t), 0.17), np.full(len(t), 0.018))
def exp_1(tl):
    if tl['distribution']['steady_pct'] < 95.0: return False, f"Steady pct {tl['distribution']['steady_pct']} < 95"
    if tl['distribution']['burst_pct'] > 0.0: return False, "Burst > 0"
    if len(tl['anomalies']) > 0: return False, "Anomalies found"
    if any(e['type'] == 'environment_change' for e in tl['events']): return False, "Env change found"
    return True, "Passed"
tests.append(("TEST 1 — Perfect Stable Sine Wave", "Baseline tracking without noise.", gen_1, exp_1))

# TEST 2 — Stable Sine Wave With Tiny Numerical Noise
def gen_2():
    t = np.arange(0, 120.0, 1.0)
    r = np.full(len(t), 0.12) + np.random.normal(0, 1e-5, len(t))
    p = np.full(len(t), 0.17) + np.random.normal(0, 1e-5, len(t))
    z = np.full(len(t), 0.018) + np.random.normal(0, 1e-6, len(t))
    return make_metrics(t, r, p, z)
def exp_2(tl):
    return exp_1(tl)
tests.append(("TEST 2 — Tiny Numerical Noise", "Robustness against floating-point noise.", gen_2, exp_2))

# TEST 3 — Realistic Sensor Jitter
def gen_3():
    t = np.arange(0, 300.0, 1.0) # 5 mins
    r = np.full(len(t), 0.12) + np.random.normal(0, 0.12*0.03, len(t))
    p = np.full(len(t), 0.17) + np.random.normal(0, 0.17*0.03, len(t))
    z = np.full(len(t), 0.018) + np.random.normal(0, 0.018*0.03, len(t))
    return make_metrics(t, r, p, z)
def exp_3(tl):
    if tl['distribution']['steady_pct'] < 80.0: return False, "Steady pct < 80"
    if tl['distribution']['burst_pct'] > 5.0: return False, "Burst > 5"
    if any(e['type'] == 'environment_change' for e in tl['events']): return False, "Env change found"
    return True, "Passed"
tests.append(("TEST 3 — Realistic Sensor Jitter", "Tolerate 3% jitter.", gen_3, exp_3))

# TEST 4 — Single Impulse Spike
def gen_4():
    t = np.arange(0, 120.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    r[60] = 0.12 * 5
    p[60] = 0.17 * 5
    return make_metrics(t, r, p, z)
def exp_4(tl):
    if tl['distribution']['steady_pct'] < 90.0: return False, "Session not mostly steady"
    if len(tl['anomalies']) < 1: return False, "Failed to detect anomaly"
    bursts = [e for e in tl['events'] if e['type'] == 'burst']
    if len(bursts) > 1: return False, "Too many bursts"
    return True, "Passed"
tests.append(("TEST 4 — Single Impulse Spike", "Isolated events do not become sustained.", gen_4, exp_4))

# TEST 5 — 5 Second Burst
def gen_5():
    t = np.arange(0, 120.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    r[60:65] = 0.12 * 5
    p[60:65] = 0.17 * 5
    return make_metrics(t, r, p, z)
def exp_5(tl):
    if tl['distribution']['burst_pct'] < 2.0: return False, "Burst not detected"
    if tl['distribution']['burst_pct'] > 15.0: return False, "Burst classified too long"
    return True, "Passed"
tests.append(("TEST 5 — 5 Second Burst", "Proper burst state hysteresis.", gen_5, exp_5))

# TEST 6 — Long Active Region
def gen_6():
    t = np.arange(0, 180.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    r[60:120] = 0.12 * 2
    return make_metrics(t, r, p, z)
def exp_6(tl):
    if tl['distribution']['active_pct'] < 20.0: return False, "Failed to detect active region"
    if tl['distribution']['burst_pct'] > 10.0: return False, "Confused active with burst"
    if not any(e['type'] == 'sustained_activity' for e in tl['events']): return False, "No sustained activity event"
    return True, "Passed"
tests.append(("TEST 6 — Long Active Region", "Active/burst separation.", gen_6, exp_6))

# TEST 7 — Environment Change
def gen_7():
    t = np.arange(0, 240.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    r[120:] = 0.30
    p[120:] = 0.40
    return make_metrics(t, r, p, z)
def exp_7(tl):
    if not any(e['type'] == 'environment_change' for e in tl['events']): return False, "No environment change event"
    if tl['insights']['baseline_drift_pct'] <= 0: return False, "Baseline drift not positive"
    return True, "Passed"
tests.append(("TEST 7 — Environment Change", "CUSUM shift detection.", gen_7, exp_7))

# TEST 8 — Slow Drift
def gen_8():
    t = np.arange(0, 300.0, 1.0)
    r = np.linspace(0.12, 0.30, len(t))
    p = np.linspace(0.17, 0.40, len(t))
    z = np.full(len(t), 0.018)
    return make_metrics(t, r, p, z)
def exp_8(tl):
    if any(e['type'] == 'environment_change' for e in tl['events']): return False, "False CUSUM trigger on drift"
    if tl['distribution']['burst_pct'] > 0: return False, "False burst on drift"
    return True, "Passed"
tests.append(("TEST 8 — Slow Drift", "Trend without CUSUM trigger.", gen_8, exp_8))

# TEST 9 — Oscillating Activity
def gen_9():
    t = np.arange(0, 300.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    for i in range(120):
        if (i // 30) % 2 == 1:
            r[i] = 0.12 * 2
    return make_metrics(t, r, p, z)
def exp_9(tl):
    if any(e['type'] == 'environment_change' for e in tl['events']): return False, "False environment change"
    if len(tl['segments']) < 5: return False, "Failed to segment oscillation"
    return True, "Passed"
tests.append(("TEST 9 — Oscillating Activity", "Segment merging structure.", gen_9, exp_9))

# TEST 10 — Quiet Session
def gen_10():
    t = np.arange(0, 120.0, 1.0)
    r = np.random.uniform(0.001, 0.005, len(t))
    p = np.random.uniform(0.002, 0.008, len(t))
    z = np.full(len(t), 0.018)
    return make_metrics(t, r, p, z)
def exp_10(tl):
    if tl['distribution']['burst_pct'] > 0: return False, "False burst in quiet"
    if tl['distribution']['active_pct'] > 10: return False, "False active in quiet"
    return True, "Passed"
tests.append(("TEST 10 — Quiet Session", "Low-amplitude classification.", gen_10, exp_10))

# TEST 11 — Missing Samples
def gen_11():
    t = np.arange(0, 120.0, 1.0)
    mask = np.random.rand(len(t)) > 0.2
    t = t[mask]
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    return make_metrics(t, r, p, z)
def exp_11(tl):
    if tl['distribution']['burst_pct'] > 0: return False, "False bursts from gaps"
    if tl['insights']['timeline_confidence'] >= 1.0: return False, "Confidence should drop"
    return True, "Passed"
tests.append(("TEST 11 — Missing Samples", "Robustness to timestamp gaps.", gen_11, exp_11))

# TEST 12 — Invalid Sample Blocks
def gen_12():
    t = np.arange(0, 120.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    r[50:60] = 0.0
    p[50:60] = 0.0
    return make_metrics(t, r, p, z)
def exp_12(tl):
    if tl['distribution']['burst_pct'] > 0: return False, "False bursts from invalid block"
    return True, "Passed"
tests.append(("TEST 12 — Invalid Sample Blocks", "Propagation of UNKNOWN state.", gen_12, exp_12))

# TEST 13 — Short Session
def gen_13():
    t = np.arange(0, 10.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    return make_metrics(t, r, p, z)
def exp_13(tl):
    if tl['distribution']['steady_pct'] != 100.0: return False, "Should be completely steady"
    return True, "Passed"
tests.append(("TEST 13 — Short Session", "Duration scaling bounds.", gen_13, exp_13))

# TEST 14 — Long Session
def gen_14():
    t = np.arange(0, 3600.0, 1.0) # 1 hour
    r = np.full(len(t), 0.12) + np.random.normal(0, 0.001, len(t))
    p = np.full(len(t), 0.17) + np.random.normal(0, 0.001, len(t))
    z = np.full(len(t), 0.018) + np.random.normal(0, 0.001, len(t))
    return make_metrics(t, r, p, z)
def exp_14(tl):
    if tl['distribution']['burst_pct'] > 0: return False, "Accumulated drift burst"
    return True, "Passed"
tests.append(("TEST 14 — Long Session", "Memory & numerical accumulation.", gen_14, exp_14))

# TEST 15 — Burst Flood Stress Test
def gen_15():
    t = np.arange(0, 1800.0, 1.0) # 30 mins
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    for _ in range(100):
        idx = np.random.randint(0, len(t)-5)
        r[idx:idx+2] = 0.8
    return make_metrics(t, r, p, z)
def exp_15(tl):
    if len(tl['anomalies']) > 150: return False, "Anomaly explosion"
    return True, "Passed"
tests.append(("TEST 15 — Burst Flood Stress Test", "Clustering limit check.", gen_15, exp_15))

# TEST 16 — ZCR-Only Disturbance
def gen_16():
    t = np.arange(0, 120.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    z[60:65] = 0.50
    return make_metrics(t, r, p, z)
def exp_16(tl):
    if len(tl['anomalies']) == 0: return False, "Failed to detect ZCR anomaly"
    if tl['distribution']['burst_pct'] > 0: return False, "False burst on ZCR"
    return True, "Passed"
tests.append(("TEST 16 — ZCR-Only Disturbance", "Feature decoupling.", gen_16, exp_16))

# TEST 17 — Peak-Only Disturbance
def gen_17():
    t = np.arange(0, 120.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    p[60] = 0.90
    return make_metrics(t, r, p, z)
def exp_17(tl):
    if len(tl['anomalies']) == 0: return False, "Failed to detect peak anomaly"
    if any(e['type'] == 'environment_change' for e in tl['events']): return False, "False env shift on peak"
    return True, "Passed"
tests.append(("TEST 17 — Peak-Only Disturbance", "Euclidean distance robustness.", gen_17, exp_17))

# TEST 26 — Long Speech (10 minutes continuous)
def gen_26():
    t = np.arange(0, 600.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    r[60:540] = 0.40 # 8 minutes of continuous loud speech
    p[60:540] = 0.60
    # Add realistic ZCR variance for speech (vowels vs consonants)
    z[60:540] = 0.018 + np.random.uniform(0.01, 0.08, 480)
    return make_metrics(t, r, p, z)
def exp_26(tl):
    if tl['distribution']['active_pct'] < 60.0: return False, "Failed to maintain ACTIVE for long duration"
    if tl['insights']['environment_state'] == 'Changing Environment': return False, "False env shift for speech"
    return True, "Passed"
tests.append(("TEST 26 — Long Continuous Speech", "Preserve ACTIVE against slow baseline.", gen_26, exp_26))

# TEST 27 — Factory Oscillation
def gen_27():
    t = np.arange(0, 600.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    for i in range(600):
        if (i // 20) % 2 == 1:
            r[i] = 0.50
    return make_metrics(t, r, p, z)
def exp_27(tl):
    if any(e['type'] == 'environment_change' for e in tl['events']): return False, "False env shift on oscillation"
    if tl['insights']['environment_state'] not in ['Highly Volatile', 'Dynamic Ambient']: return False, "Did not detect high volatility"
    return True, "Passed"
tests.append(("TEST 27 — Factory Oscillation", "Suppress CUSUM on heavy transition density.", gen_27, exp_27))

# TEST 28 — Speech + Packet Loss
def gen_28():
    t = np.arange(0, 300.0, 1.0)
    # 30% random packet loss
    mask = np.random.rand(len(t)) > 0.3
    t = t[mask]
    r = np.full(len(t), 0.12)
    r[(t > 60) & (t < 240)] = 0.40
    p = r * 1.5
    z = np.full(len(t), 0.018)
    return make_metrics(t, r, p, z)
def exp_28(tl):
    if tl['insights']['timeline_confidence'] > 0.8: return False, "Confidence should be reduced due to 30% missing data"
    if tl['distribution']['active_pct'] < 40.0: return False, "Lost ACTIVE state due to packet loss"
    return True, "Passed"
tests.append(("TEST 28 — Speech + Packet Loss", "Confidence reflects duration/sample coverage.", gen_28, exp_28))

# TEST 29 — Environment Shift + Speech
def gen_29():
    t = np.arange(0, 600.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    # HVAC turns on at 200s
    r[200:] = 0.30
    p[200:] = 0.45
    # Speech starts at 300s
    r[300:400] = 0.60
    p[300:400] = 0.90
    return make_metrics(t, r, p, z)
def exp_29(tl):
    if not any(e['type'] == 'environment_change' for e in tl['events']): return False, "Failed to detect HVAC env shift"
    if tl['distribution']['active_pct'] < 10.0: return False, "Failed to detect speech as ACTIVE against new HVAC baseline"
    return True, "Passed"
tests.append(("TEST 29 — Env Shift + Speech", "Separate environment shift from active deviations.", gen_29, exp_29))

# TEST 30 — Burst Inside Active Region
def gen_30():
    t = np.arange(0, 300.0, 1.0)
    r = np.full(len(t), 0.12)
    p = np.full(len(t), 0.17)
    z = np.full(len(t), 0.018)
    # Speech
    r[100:200] = 0.40
    p[100:200] = 0.60
    # Door Slam (High gradient transient)
    r[150] = 1.2
    p[150] = 1.8
    r[151] = 0.8
    p[151] = 1.2
    return make_metrics(t, r, p, z)
def exp_30(tl):
    if tl['distribution']['active_pct'] < 20.0: return False, "Failed to maintain active region"
    if tl['distribution']['burst_pct'] == 0.0: return False, "Failed to detect door slam burst inside active region"
    return True, "Passed"
tests.append(("TEST 30 — Burst Inside Active", "Detect transient gradient burst within elevated slow baseline.", gen_30, exp_30))

results = []
for name, desc, gen, exp in tests:
    print(f"Running {name}...")
    res = evaluate_test(name, desc, gen, exp)
    results.append(res)

with open('audit_results.json', 'w') as f:
    json.dump([{k: v for k, v in r.items() if k != 'tl'} for r in results], f, indent=2)
print("Audit complete.")
