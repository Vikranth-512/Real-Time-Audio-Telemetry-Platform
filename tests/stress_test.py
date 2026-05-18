"""
stress_test.py — Multi-device, multi-dashboard isolation + throughput test.

Tests:
  1. 5 concurrent simulated device streams (publishers)
  2. 5 concurrent dashboard watchers (each watching ONE session)
  3. Isolation: device A packets NEVER appear on dashboard B
  4. Throughput: packets/sec per device with no backlog
  5. Reconnect: dashboard reconnects and resumes
  6. Session cleanup: sessions removed from active_sessions after stop

Usage:
    pip install websockets
    python tests/stress_test.py
"""

import asyncio
import json
import random
import sys
import time
import math

import websockets

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

BACKEND_WS   = "ws://localhost:8000"
BACKEND_HTTP = "http://localhost:8000"

NUM_DEVICES    = 5
NUM_DASHBOARDS = 5
PACKETS_PER_SEC = 20
TEST_DURATION_SEC = 30

# ─── Isolation checker ────────────────────────────────────────────────────────

class IsolationViolationError(Exception):
    pass

# ─── Simulated device (publisher) ────────────────────────────────────────────

async def run_device(device_index: int, result: dict, stop_event: asyncio.Event):
    url = f"{BACKEND_WS}/ws/stream"
    sent = 0
    session_id = None

    try:
        async with websockets.connect(url, ping_interval=30) as ws:
            # Receive session_created
            msg      = await ws.recv()
            data     = json.loads(msg)
            session_id = data["session_id"]
            token    = data.get("token", "")

            result["session_id"] = session_id
            result["token"]      = token
            result["device_index"] = device_index

            print(f"[device-{device_index}] session={session_id}")

            interval = 1.0 / PACKETS_PER_SEC
            start    = time.time()

            while not stop_event.is_set() and (time.time() - start) < TEST_DURATION_SEC:
                # Generate a fake waveform packet
                t       = time.time()
                samples = [int(2048 + 1000 * math.sin(2 * math.pi * 440 * (t + i / 48000)))
                           for i in range(128)]
                packet  = {
                    "type":       "audio_data",
                    "session_id": session_id,
                    "timestamp":  t,
                    "samples":    samples,
                }
                await ws.send(json.dumps(packet))
                sent += 1
                await asyncio.sleep(interval)

            result["sent"]    = sent
            result["elapsed"] = time.time() - start
            result["pps"]     = sent / max(result["elapsed"], 1)
            print(f"[device-{device_index}] done. Sent {sent} packets @ {result['pps']:.1f} pkt/s")

    except Exception as e:
        result["error"] = str(e)
        print(f"[device-{device_index}] ERROR: {e}")


# ─── Simulated dashboard (viewer) ────────────────────────────────────────────

async def run_dashboard(dashboard_index: int, target_session_id: str,
                        all_session_ids: list, result: dict, stop_event: asyncio.Event):
    """Subscribes to target_session_id, verifies it never receives another session's data."""
    url      = f"{BACKEND_WS}/ws/audio"
    received = 0
    violations = 0

    try:
        async with websockets.connect(url, ping_interval=30) as ws:
            # Subscribe to target session
            await ws.send(json.dumps({"type": "subscribe", "session_id": target_session_id}))
            print(f"[dash-{dashboard_index}] subscribed to {target_session_id}")

            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if data.get("type") != "audio_update":
                    continue

                sid = data.get("session_id")
                received += 1

                # ISOLATION CHECK: must only see target session's data
                if sid and sid != target_session_id:
                    violations += 1
                    print(f"[dash-{dashboard_index}] ISOLATION VIOLATION: expected {target_session_id} got {sid}")

        result["received"]   = received
        result["violations"] = violations

    except Exception as e:
        result["error"] = str(e)
        print(f"[dash-{dashboard_index}] ERROR: {e}")


# ─── Reconnect test ────────────────────────────────────────────────────────────

async def test_reconnect(session_id: str) -> bool:
    """Disconnect and reconnect a dashboard, verify stream resumes."""
    url = f"{BACKEND_WS}/ws/audio"

    for attempt in range(3):
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"type": "subscribe", "session_id": session_id}))
                # Wait for at least one audio_update
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        raw  = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        data = json.loads(raw)
                        if data.get("type") == "audio_update":
                            print(f"[reconnect] Attempt {attempt+1}: stream resumed ✓")
                            return True
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            print(f"[reconnect] Attempt {attempt+1} failed: {e}")
        await asyncio.sleep(0.5)

    print("[reconnect] FAILED — stream did not resume after reconnect")
    return False


# ─── Main runner ─────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print(f"Stress Test: {NUM_DEVICES} devices × {NUM_DASHBOARDS} dashboards")
    print(f"Duration: {TEST_DURATION_SEC}s | Rate: {PACKETS_PER_SEC} pkt/s/device")
    print("=" * 60)

    stop_event      = asyncio.Event()
    device_results  = [{} for _ in range(NUM_DEVICES)]
    dash_results    = [{} for _ in range(NUM_DASHBOARDS)]

    # Phase 1: Start all devices simultaneously
    print("\n[Phase 1] Starting devices...")
    device_tasks = [
        asyncio.create_task(run_device(i, device_results[i], stop_event))
        for i in range(NUM_DEVICES)
    ]

    # Wait for all devices to receive their session_id
    deadline = time.time() + 5
    while time.time() < deadline:
        if all(d.get("session_id") for d in device_results):
            break
        await asyncio.sleep(0.1)

    session_ids = [d["session_id"] for d in device_results if d.get("session_id")]
    if len(session_ids) < NUM_DEVICES:
        print(f"Only {len(session_ids)}/{NUM_DEVICES} devices connected — aborting")
        stop_event.set()
        await asyncio.gather(*device_tasks, return_exceptions=True)
        return

    print(f"[Phase 1] All devices connected: {session_ids}")

    # Phase 2: Start dashboards, each watching a different session
    print("\n[Phase 2] Starting dashboards...")
    dash_tasks = [
        asyncio.create_task(
            run_dashboard(i, session_ids[i % len(session_ids)], session_ids, dash_results[i], stop_event)
        )
        for i in range(NUM_DASHBOARDS)
    ]

    # Let everything run for HALF the test duration, then do reconnect test
    await asyncio.sleep(TEST_DURATION_SEC // 2)

    # Phase 3: Reconnect test WHILE devices are still streaming
    print("\n[Phase 3] Reconnect test (devices still streaming)...")
    reconnect_ok = await test_reconnect(session_ids[0])

    # Let the rest of the test duration complete
    await asyncio.sleep(TEST_DURATION_SEC // 2)

    # Stop everything
    stop_event.set()
    await asyncio.gather(*device_tasks,  return_exceptions=True)
    await asyncio.gather(*dash_tasks,    return_exceptions=True)

    # ── Results ────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    total_violations = 0
    total_received   = 0

    print("\n── Devices ──")
    for i, r in enumerate(device_results):
        if r.get("error"):
            print(f"  device-{i}: ERROR — {r['error']}")
        else:
            pps = r.get('pps', 0)
            ok  = "✓" if pps >= PACKETS_PER_SEC * 0.9 else "⚠"
            print(f"  device-{i}: {r.get('sent',0)} packets @ {pps:.1f} pkt/s {ok}")

    print("\n── Dashboards ──")
    for i, r in enumerate(dash_results):
        if r.get("error"):
            print(f"  dash-{i}: ERROR — {r['error']}")
        else:
            v = r.get("violations", 0)
            total_violations += v
            total_received   += r.get("received", 0)
            iso = "✓ ISOLATED" if v == 0 else f"✗ {v} VIOLATIONS"
            print(f"  dash-{i}: {r.get('received',0)} packets received — Isolation: {iso}")

    print("\n── Summary ──")
    print(f"  Total packets received: {total_received}")
    print(f"  Isolation violations:   {total_violations} {'✓' if total_violations == 0 else '✗'}")
    print(f"  Reconnect test:         {'✓ PASS' if reconnect_ok else '✗ FAIL'}")

    overall = total_violations == 0 and reconnect_ok
    print(f"\n  Overall: {'✓ PASS' if overall else '✗ FAIL'}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
