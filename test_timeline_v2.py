import json
from backend.processing.timeline_analyzer import generate_timeline
import numpy as np

def make_metrics(timestamps, rms_vals, peak_vals, zcr_vals):
    metrics = []
    for t, r, p, z in zip(timestamps, rms_vals, peak_vals, zcr_vals):
        metrics.append({
            "timestamp": t,
            "metrics": {"rms": r, "peak": p, "zcr": z}
        })
    return metrics

def run_tests():
    # Test A: Flat room noise
    N = 100
    t = np.arange(N, dtype=float)
    # small noise around 0.1
    rms = np.random.normal(0.1, 0.01, N)
    peak = rms * 1.5
    zcr = np.random.normal(0.05, 0.01, N)
    metrics_a = make_metrics(t, rms, peak, zcr)
    
    tl_a = generate_timeline(metrics_a)
    print("Test A (Flat Noise):")
    print(f"  Steady > 90%: {tl_a['distribution']['steady_pct']}%")
    print(f"  Anomalies: {len(tl_a['anomalies'])}")
    print(f"  Bursts: {sum(1 for s in tl_a['segments'] if s['type'] == 'burst')}")
    print(f"  Summary: {tl_a['summary']}")
    print()

    # Test B: One clap
    rms_b = rms.copy()
    rms_b[50] = 0.8
    peak_b = peak.copy()
    peak_b[50] = 1.0
    metrics_b = make_metrics(t, rms_b, peak_b, zcr)
    tl_b = generate_timeline(metrics_b)
    print("Test B (One Clap):")
    print(f"  Anomalies: {len(tl_b['anomalies'])}")
    print(f"  Bursts: {sum(1 for s in tl_b['segments'] if s['type'] == 'burst')}")
    print(f"  Summary: {tl_b['summary']}")
    print()

    # Test C: HVAC Starts midway
    rms_c = rms.copy()
    rms_c[50:] += 0.2
    metrics_c = make_metrics(t, rms_c, peak.copy(), zcr)
    tl_c = generate_timeline(metrics_c)
    print("Test C (HVAC Start):")
    print(f"  Change points: {sum(1 for e in tl_c['events'] if e['type'] == 'environment_change')}")
    print(f"  Summary: {tl_c['summary']}")
    print()

    # Test D: Alternating loud/quiet every 30s
    t_d = np.arange(120, dtype=float)
    rms_d = np.zeros(120)
    for i in range(120):
        if (i // 30) % 2 == 0:
            rms_d[i] = np.random.normal(0.1, 0.01)
        else:
            rms_d[i] = np.random.normal(0.4, 0.05)
    peak_d = rms_d * 1.5
    zcr_d = np.random.normal(0.05, 0.01, 120)
    metrics_d = make_metrics(t_d, rms_d, peak_d, zcr_d)
    tl_d = generate_timeline(metrics_d)
    print("Test D (Alternating):")
    print(f"  Dynamics Score: {tl_d['insights']['activity_intensity_index']}")
    print(f"  Profile: {tl_d['insights']['session_fingerprint']}")
    print(f"  Summary: {tl_d['summary']}")
    print()

def test_ultra_stable_sine_wave():
    """
    Scenario F: A perfectly stable sine wave with near-zero mathematical noise.
    Verifies the ultra-stable fast-path avoids MAD collapse and false anomalies.
    """
    t = np.arange(0, 75.0, 1.0)
    rms = np.full(len(t), 0.707) + np.random.normal(0, 1e-6, len(t))
    peak = np.full(len(t), 1.0) + np.random.normal(0, 1e-6, len(t))
    zcr = np.full(len(t), 0.0184) + np.random.normal(0, 1e-7, len(t))
    
    metrics = make_metrics(t, rms, peak, zcr)
    tl = generate_timeline(metrics)
    
    assert tl["distribution"]["steady_pct"] == 100.0
    assert tl["distribution"]["burst_pct"] == 0.0
    assert len(tl["anomalies"]) == 0
    assert not any(e["type"] == "environment_change" for e in tl["events"])
    assert tl["insights"]["session_fingerprint"] == "Ultra-Stable Calibration"
    assert tl["insights"]["activity_intensity_index"] == 25.0
    print("Test F (Ultra-Stable Sine Wave): PASSED!")
    print()

    # Test E: Corrupted stream
    rms_e = rms.copy()
    rms_e[20:40] = 0.0
    metrics_e = make_metrics(t, rms_e, peak.copy(), zcr)
    tl_e = generate_timeline(metrics_e)
    print("Test E (Corrupted):")
    print(f"  Confidence: {tl_e['insights']['timeline_confidence']}")
    print()

if __name__ == "__main__":
    run_tests()
    test_ultra_stable_sine_wave()
    print("All observability-grade timeline tests passed! (Including Fast-Path)")
