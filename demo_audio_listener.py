import asyncio
import websockets
import json
import time
import uuid
import pyaudio
import numpy as np

WS_URL = "ws://localhost:8000/ws/stream"

SAMPLE_RATE = 48000
CHANNELS = 2
DEVICE_INDEX = 12
PACKET_SIZE = 128

OFFSET = 2048

RMS_THRESHOLD = 100

# =========================
# 🔍 DEVICE LISTING
# =========================
audio = pyaudio.PyAudio()

print("\nAvailable audio input devices:\n")

for i in range(audio.get_device_count()):
    info = audio.get_device_info_by_index(i)
    print(f"{i}: {info['name']}")

DEVICE_INDEX = int(input("\nEnter input device index: "))


# =========================
# 🎤 AUDIO STREAM
# =========================

stream = audio.open(
    format=pyaudio.paInt16,
    channels=CHANNELS,
    rate=SAMPLE_RATE,
    input=True,
    input_device_index=DEVICE_INDEX,
    frames_per_buffer=PACKET_SIZE
)

# =========================
# 🧠 PROCESSING + DIAGNOSTICS
# =========================
def process_samples(raw):

    samples = raw.astype(np.float32)

    # Remove DC offset
    samples -= np.mean(samples)

    rms = np.sqrt(np.mean(samples ** 2))
    peak = np.max(np.abs(samples))

    print(f"RMS: {int(rms):5d} | PEAK: {int(peak):5d}", end="\r")

    # Silence detection
    if rms < RMS_THRESHOLD:
        return np.full(len(samples), OFFSET, dtype=np.int16), "SILENCE"

    # Static scaling: map 16-bit audio (-32768 to 32767) to 12-bit audio (-2047 to 2047)
    # This preserves relative amplitude differences between quiet and loud packets
    samples = (samples / 32768.0) * 2047.0

    samples = samples + OFFSET
    samples = np.clip(samples, 0, 4095)

    return samples.astype(np.int16), "SIGNAL"


# =========================
# 📡 STREAM LOOP
# =========================
async def send_audio():

    async with websockets.connect(WS_URL) as ws:

        print("\nConnected to backend")

        # RECEIVE SESSION ID FROM SERVER
        msg = await ws.recv()
        data = json.loads(msg)

        session_id = data["session_id"]   # USE SERVER SESSION
        print("Assigned Session ID:", session_id)
        packet_count = 0

        while True:

            data = stream.read(PACKET_SIZE, exception_on_overflow=False)

            raw = np.frombuffer(data, dtype=np.int16)

            raw = raw.reshape(-1, 2)
            raw_samples = raw.mean(axis=1)

            samples, state = process_samples(raw_samples)

            packet = {
                "type": "audio_data",
                "session_id": session_id,
                "timestamp": time.time(),
                "sample_rate": SAMPLE_RATE,
                "waveform": "live_audio",
                "state": state,
                "samples": samples.tolist()
            }

            await ws.send(json.dumps(packet))

            packet_count += 1

            if packet_count % 20 == 0:
                print(f"\nPackets sent: {packet_count} | State: {state}")

            await asyncio.sleep(PACKET_SIZE / SAMPLE_RATE)


# =========================
# 🔁 MAIN LOOP
# =========================
async def main():

    print("\n--- AUDIO DIAGNOSTIC STREAM ---")
    print("debug print")


    while True:
        try:
            await send_audio()
        except Exception as e:
            print("\nConnection error:", e)
            print("Retrying in 2 seconds...\n")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())