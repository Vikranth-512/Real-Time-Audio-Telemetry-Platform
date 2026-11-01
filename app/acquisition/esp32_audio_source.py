"""
esp32_audio_source.py
---------------------
Acquires PCM audio samples streamed from an ESP32 microcontroller over USB-Serial.

Framed Binary Wire Protocol:
    Offset  Size    Field
    0       2       MAGIC = 0xAA55 (uint16 little-endian, wire bytes: 0x55 0xAA)
    2       2       SAMPLE_COUNT (uint16 little-endian, 1 <= N <= 4096)
    4       2N      PCM_PAYLOAD (signed int16 little-endian, mono)
    4+2N    2       CRC16 (uint16 little-endian, CRC-16-CCITT over [0 : 4+2N])

Downstream Contract:
    Decodes signed 16-bit PCM into float32 (sample / 32768.0), accumulates samples
    into ANALYZER_FRAME_SIZE chunks (1024 samples), and enqueues (capture_time, frame)
    tuples into the application capture_queue for AnalyzerWorker consumption.

    No signal modification, dynamic normalization, gain, or DSP filtering is applied.
"""

import enum
import logging
import queue
import struct
import time
from typing import Optional, List, Dict, Any

import numpy as np
import serial
from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)

# ── Wire Protocol Constants ───────────────────────────────────────────────────
MAGIC: int = 0xAA55
WIRE_MAGIC_BYTES: bytes = struct.pack('<H', MAGIC)  # b'\x55\xaa'
HEADER_SIZE: int = 4        # MAGIC (2B) + SAMPLE_COUNT (2B)
TRAILER_SIZE: int = 2       # CRC16 (2B)
MIN_SAMPLE_COUNT: int = 1
MAX_SAMPLE_COUNT: int = 4096
MAX_PAYLOAD_BYTES: int = MAX_SAMPLE_COUNT * 2  # 8192 bytes
MIN_FRAME_BYTES: int = HEADER_SIZE + (MIN_SAMPLE_COUNT * 2) + TRAILER_SIZE
MAX_FRAME_BYTES: int = HEADER_SIZE + MAX_PAYLOAD_BYTES + TRAILER_SIZE

# Downstream delivery frame size expected by AnalyzerWorker
ANALYZER_FRAME_SIZE: int = 1024


# ── Fast CRC16-CCITT (Polynomial 0x1021, Initial 0xFFFF) ──────────────────────
_CRC16_TABLE = []
for _i in range(256):
    _curr = _i << 8
    for _ in range(8):
        if _curr & 0x8000:
            _curr = ((_curr << 1) ^ 0x1021) & 0xFFFF
        else:
            _curr = (_curr << 1) & 0xFFFF
    _CRC16_TABLE.append(_curr)


def compute_crc16(data: bytes | bytearray | memoryview) -> int:
    """Compute 16-bit CRC-CCITT (poly 0x1021, init 0xFFFF) over data."""
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFF00) ^ _CRC16_TABLE[(crc >> 8) ^ byte]
    return crc & 0xFFFF


def encode_esp32_packet(samples: np.ndarray | list[float | int], use_crc: bool = True) -> bytes:
    """
    Encode an array of PCM samples into the binary wire protocol packet.
    Useful for unit tests, simulated serial streams, and firmware reference.
    """
    arr = np.asarray(samples)
    if np.issubdtype(arr.dtype, np.floating):
        raw_int16 = np.clip(np.round(arr * 32768.0), -32768, 32767).astype('<i2')
    else:
        raw_int16 = arr.astype('<i2')

    sample_count = len(raw_int16)
    if not (MIN_SAMPLE_COUNT <= sample_count <= MAX_SAMPLE_COUNT):
        raise ValueError(f"Sample count {sample_count} out of bounds [{MIN_SAMPLE_COUNT}, {MAX_SAMPLE_COUNT}]")

    header = struct.pack('<HH', MAGIC, sample_count)
    payload = raw_int16.tobytes()
    packet = bytearray(header + payload)
    if use_crc:
        crc = compute_crc16(packet)
        packet.extend(struct.pack('<H', crc))
    return bytes(packet)


# ── Packet Parser ─────────────────────────────────────────────────────────────

class Esp32PacketParser:
    """
    Robust byte-stream framing and packet decoder.
    Maintains a persistent byte buffer, searches for WIRE_MAGIC_BYTES,
    validates CRC16, and decodes int16 PCM into float32 NumPy arrays.
    """

    def __init__(self, use_crc: bool = True):
        self._buffer = bytearray()
        self._use_crc = use_crc

        # Diagnostics counters
        self.packets_decoded = 0
        self.samples_decoded = 0
        self.bytes_received = 0
        self.invalid_packets = 0
        self.crc_failures = 0
        self.resync_events = 0

    def reset(self) -> None:
        """Clear parser buffer and reset state."""
        self._buffer.clear()

    def feed(self, data: bytes) -> List[np.ndarray]:
        """
        Feed raw serial bytes into the parser.
        Returns a list of decoded 1D float32 NumPy arrays.
        """
        if not data:
            return []

        self._buffer.extend(data)
        self.bytes_received += len(data)
        decoded_frames: List[np.ndarray] = []

        while True:
            # Need at least HEADER_SIZE to evaluate
            if len(self._buffer) < HEADER_SIZE:
                # Retain final 1 byte if it could be the first magic byte (0x55)
                if len(self._buffer) == 1 and self._buffer[0] != WIRE_MAGIC_BYTES[0]:
                    self._buffer.clear()
                break

            # Search for MAGIC
            idx = self._buffer.find(WIRE_MAGIC_BYTES)
            if idx == -1:
                # No complete magic found.
                self.resync_events += 1
                # Retain the final 1 byte if it could be the start of WIRE_MAGIC_BYTES
                if self._buffer[-1] == WIRE_MAGIC_BYTES[0]:
                    self._buffer = bytearray([WIRE_MAGIC_BYTES[0]])
                else:
                    self._buffer.clear()
                break

            if idx > 0:
                # Discard noise preceding MAGIC
                self.resync_events += 1
                del self._buffer[:idx]
                if len(self._buffer) < HEADER_SIZE:
                    break

            # Now self._buffer starts with WIRE_MAGIC_BYTES
            magic, sample_count = struct.unpack_from('<HH', self._buffer, 0)

            # Validate sample_count bounds
            if not (MIN_SAMPLE_COUNT <= sample_count <= MAX_SAMPLE_COUNT):
                self.invalid_packets += 1
                # Skip past this false magic to continue searching
                del self._buffer[:2]
                continue

            payload_bytes = sample_count * 2
            expected_frame_bytes = HEADER_SIZE + payload_bytes + (TRAILER_SIZE if self._use_crc else 0)

            if len(self._buffer) < expected_frame_bytes:
                # Incomplete payload / trailer.
                # If a subsequent MAGIC arrives before this frame finished,
                # the current frame was truncated or corrupted in transit.
                next_magic = self._buffer.find(WIRE_MAGIC_BYTES, 2)
                if next_magic != -1:
                    self.invalid_packets += 1
                    self.resync_events += 1
                    del self._buffer[:next_magic]
                    continue
                # Wait for subsequent serial bytes
                break

            # Validate CRC16 if enabled
            if self._use_crc:
                data_span = self._buffer[:HEADER_SIZE + payload_bytes]
                computed_crc = compute_crc16(data_span)
                trailer_crc, = struct.unpack_from('<H', self._buffer, HEADER_SIZE + payload_bytes)
                if computed_crc != trailer_crc:
                    self.crc_failures += 1
                    logger.debug(f"CRC mismatch: computed=0x{computed_crc:04X}, trailer=0x{trailer_crc:04X}")
                    # Corrupted payload or false header: advance past magic to resynchronize
                    del self._buffer[:2]
                    continue

            # Extract payload
            payload = bytes(self._buffer[HEADER_SIZE:HEADER_SIZE + payload_bytes])
            del self._buffer[:expected_frame_bytes]

            # Decode signed int16 PCM -> float32
            # Explicit conversion factor (1.0 / 32768.0)
            raw = np.frombuffer(payload, dtype='<i2')
            float_samples = (raw.astype(np.float32) / 32768.0)

            self.packets_decoded += 1
            self.samples_decoded += len(float_samples)
            decoded_frames.append(float_samples)

        return decoded_frames


# ── ESP32 Lifecycle States ────────────────────────────────────────────────────

class Esp32State(str, enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING   = "CONNECTING"
    CONNECTED    = "CONNECTED"    # COM port open, waiting for first packet
    RUNNING      = "RUNNING"      # Receiving valid PCM stream
    STOPPING     = "STOPPING"
    ERROR        = "ERROR"


# ── ESP32 Audio Source Worker ─────────────────────────────────────────────────

class Esp32AudioSource(QObject):
    """
    QObject worker running serial communication on a dedicated worker QThread.
    Reads serial chunks, passes them through Esp32PacketParser, accumulates
    decoded samples into ANALYZER_FRAME_SIZE (1024) chunks, and deposits them
    into the downstream capture_queue.
    """

    state_changed  = Signal(str)         # Esp32State value
    status_message = Signal(str)         # Human-readable status details
    stats_updated  = Signal(dict)        # Diagnostics dictionary
    error_occurred = Signal(str)         # Error message

    def __init__(
        self,
        out_queue: queue.Queue,
        port: str,
        baud_rate: int = 115200,
        sample_rate: int = 48000,
        channels: int = 1,
        analyzer_frame_size: int = ANALYZER_FRAME_SIZE,
        use_crc: bool = True,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._queue               = out_queue
        self._port                = port
        self._baud_rate           = int(baud_rate)
        self._sample_rate         = int(sample_rate)
        self._channels            = int(channels)
        self._frame_size          = int(analyzer_frame_size)
        self._use_crc             = use_crc

        self._parser              = Esp32PacketParser(use_crc=self._use_crc)
        self._serial: Optional[serial.Serial] = None
        self._running             = False
        self._state               = Esp32State.DISCONNECTED

        # Backpressure & diagnostics
        self._queue_drops         = 0
        self._last_packet_time    = 0.0
        self._has_received_first_packet = False

        # Accumulator for matching downstream frame size
        self._accumulator        = np.empty(0, dtype=np.float32)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def port(self) -> str:
        return self._port

    @property
    def baud_rate(self) -> int:
        return self._baud_rate

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def state(self) -> Esp32State:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._running

    def _set_state(self, new_state: Esp32State, msg: str = "") -> None:
        self._state = new_state
        self.state_changed.emit(new_state.value)
        if msg:
            self.status_message.emit(msg)
        logger.info(f"Esp32AudioSource [{self._port}]: state={new_state.value} ({msg})")

    # ── Worker Execution Loop ─────────────────────────────────────────────────

    @Slot()
    def run(self) -> None:
        """Main worker execution loop called on the worker QThread."""
        self._running = True
        self._has_received_first_packet = False
        self._accumulator = np.empty(0, dtype=np.float32)
        self._parser.reset()
        self._set_state(Esp32State.CONNECTING, f"Opening serial port {self._port} at {self._baud_rate} baud...")

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,  # 100 ms read timeout for responsive stop
            )
            # Flush existing stale bytes from device buffer
            self._serial.reset_input_buffer()
            self._set_state(Esp32State.CONNECTED, f"Connected to {self._port} — waiting for PCM stream")
        except Exception as exc:
            logger.error(f"Failed to open serial port {self._port}: {exc}")
            self._running = False
            self._set_state(Esp32State.ERROR, f"Failed to open {self._port}: {exc}")
            self.error_occurred.emit(str(exc))
            return

        last_stats_emit = time.monotonic()

        while self._running:
            try:
                # Read with finite timeout
                chunk = self._serial.read(1024)
                if chunk:
                    frames = self._parser.feed(chunk)
                    for frame in frames:
                        now = time.time()
                        self._last_packet_time = now

                        if not self._has_received_first_packet:
                            self._has_received_first_packet = True
                            self._set_state(
                                Esp32State.RUNNING,
                                f"Receiving PCM ({self._sample_rate / 1000:.1f} kHz interpretation)"
                            )

                        # Accumulate samples
                        if len(self._accumulator) == 0:
                            self._accumulator = frame
                        else:
                            self._accumulator = np.concatenate([self._accumulator, frame])

                        # Emit complete ANALYZER_FRAME_SIZE chunks
                        while len(self._accumulator) >= self._frame_size:
                            delivery_chunk = self._accumulator[:self._frame_size]
                            self._accumulator = self._accumulator[self._frame_size:]

                            capture_time = now
                            try:
                                self._queue.put_nowait((capture_time, delivery_chunk))
                            except queue.Full:
                                # Real-time backpressure: drop oldest frame to maintain low latency
                                try:
                                    self._queue.get_nowait()
                                except queue.Empty:
                                    pass
                                self._queue.put_nowait((capture_time, delivery_chunk))
                                self._queue_drops += 1
                                logger.debug(f"Esp32AudioSource: queue full, oldest frame dropped (total={self._queue_drops})")

                # Throttle diagnostics updates to ~4 Hz
                mono_now = time.monotonic()
                if mono_now - last_stats_emit >= 0.25:
                    last_stats_emit = mono_now
                    self._emit_diagnostics()

            except serial.SerialException as exc:
                if not self._running:
                    break
                logger.error(f"SerialException on {self._port}: {exc}")
                self._running = False
                self._set_state(Esp32State.ERROR, f"Serial disconnected: {exc}")
                self.error_occurred.emit(str(exc))
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.exception(f"Unexpected error in ESP32 reader loop: {exc}")
                self._running = False
                self._set_state(Esp32State.ERROR, f"Worker error: {exc}")
                self.error_occurred.emit(str(exc))
                break

        # Cleanup serial port
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception as e:
                logger.debug(f"Error closing serial port: {e}")
            self._serial = None

        if self._state != Esp32State.ERROR:
            self._set_state(Esp32State.DISCONNECTED, "Disconnected")

    @Slot()
    def stop(self) -> None:
        """Signal the reader loop to stop."""
        self._running = False
        if self._state not in (Esp32State.ERROR, Esp32State.DISCONNECTED):
            self._set_state(Esp32State.STOPPING, "Stopping serial ingestion...")
        # Close serial port immediately to unblock any pending read
        if self._serial is not None:
            try:
                if self._serial.is_open:
                    self._serial.close()
            except Exception:
                pass

    def _emit_diagnostics(self) -> None:
        stats = {
            "port": self._port,
            "state": self._state.value,
            "packets_received": self._parser.packets_decoded,
            "samples_received": self._parser.samples_decoded,
            "bytes_received": self._parser.bytes_received,
            "invalid_packets": self._parser.invalid_packets,
            "crc_failures": self._parser.crc_failures,
            "resync_events": self._parser.resync_events,
            "queue_drops": self._queue_drops,
            "last_packet_time": self._last_packet_time,
        }
        self.stats_updated.emit(stats)
