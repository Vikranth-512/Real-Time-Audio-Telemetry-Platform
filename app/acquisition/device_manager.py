"""
device_manager.py
-----------------
Enumerate and validate audio capture devices via pyaudiowpatch.
Provides WASAPI Loopback support for capturing desktop (system) audio.

Each device is classified as either 'System Output' or 'Microphone' and
given a clean display_name that strips backend implementation details.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

import pyaudiowpatch as pyaudio
import serial
from serial.tools import list_ports

logger = logging.getLogger(__name__)

# Known USB-UART / ESP32 Vendor IDs (hints, not authoritative)
ESP32_HINT_VIDS = {
    0x303A,  # Espressif Systems (native USB JTAG/serial, CDC, S2, S3, C3)
    0x10C4,  # Silicon Labs (CP210x, common on ESP32 boards)
    0x1A86,  # QinHeng Electronics (CH340/CH341, common on ESP32 boards)
    0x0403,  # FTDI (FT232R)
}

# ── Application defaults ──────────────────────────────────────────────────────
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS    = 1


# ── Name utilities ────────────────────────────────────────────────────────────

def _clean_display_name(name: str) -> str:
    """Strip trademark symbols, '[Loopback]', and backend suffixes for UI."""
    name = name.replace("(R)", "").replace("®", "").replace("(r)", "")
    name = re.sub(r"\[Loopback\]", "", name, flags=re.IGNORECASE)
    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name).strip()
    # Truncate very long names (rare on modern drivers)
    if len(name) > 50:
        name = name[:47] + "…"
    return name


def _classify_source_type(raw_name: str, is_loopback: bool) -> str:
    """
    Return 'System Output' for devices that capture rendered desktop audio,
    or 'Microphone' for acoustic/physical input devices.
    """
    n = raw_name.lower()
    if is_loopback:
        return "System Output"
    if "stereo mix" in n:
        return "System Output"
    if "primary sound" in n or "sound mapper" in n:
        # These shadow the default device — treat as system output
        return "System Output"
    # Explicit acoustic/physical input markers
    if any(k in n for k in ("microphone", "mic ", "line in", "line-in", "input")):
        return "Microphone"
    return "Microphone"   # safe default for unknown input devices


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AudioDevice:
    index:               int
    name:                str      # raw name from pyaudio API
    display_name:        str      # clean name for UI combo boxes
    max_input_channels:  int
    default_sample_rate: float
    host_api_name:       str
    source_type:         str      # "System Output" or "Microphone"
    is_loopback:         bool     # True = WASAPI loopback (pre-endpoint-volume)


@dataclass(frozen=True)
class SerialDeviceInfo:
    port:          str             # e.g. "COM15"
    description:   str             # Raw OS description
    hwid:          str             # Hardware ID
    vid:           Optional[int]   # USB Vendor ID (int) or None
    pid:           Optional[int]   # USB Product ID (int) or None
    manufacturer:  str             # Manufacturer string
    serial_number: str             # Serial number string
    is_esp32_hint: bool            # True if description/VID/PID hints at ESP32
    display_name:  str             # Clean formatted name for UI


# ── Manager ───────────────────────────────────────────────────────────────────

class AudioDeviceManager:
    """
    Wraps pyaudiowpatch device enumeration.
    Provides WASAPI Loopback support and clean device classification.
    All public methods are read-only and safe to call from the GUI thread.
    """

    def __init__(self):
        self._pa = pyaudio.PyAudio()

    def __del__(self):
        try:
            self._pa.terminate()
        except Exception:
            pass

    # ── Enumeration ───────────────────────────────────────────────────────────

    def enumerate_input_devices(self) -> list[AudioDevice]:
        """Return all usable input devices, including WASAPI loopback devices."""
        devices = []
        try:
            wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                host_api = self._pa.get_host_api_info_by_index(info["hostApi"])["name"]

                is_input    = info["maxInputChannels"] > 0
                is_loopback = (
                    info["hostApi"] == wasapi_info["index"]
                    and info.get("isLoopbackDevice", False)
                )

                if not (is_input or is_loopback):
                    continue

                raw_name  = info["name"]
                disp_name = _clean_display_name(raw_name)
                src_type  = _classify_source_type(raw_name, is_loopback)

                devices.append(AudioDevice(
                    index=info["index"],
                    name=raw_name,
                    display_name=disp_name,
                    max_input_channels=info.get("maxInputChannels", 2),
                    default_sample_rate=info["defaultSampleRate"],
                    host_api_name=host_api,
                    source_type=src_type,
                    is_loopback=is_loopback,
                ))
        except Exception as exc:
            logger.error(f"Failed to enumerate audio devices: {exc}")
        return devices

    def get_default_input_device(self) -> Optional[AudioDevice]:
        """Return the OS default input device, or None if unavailable."""
        try:
            default_idx = self._pa.get_default_input_device_info()["index"]
            info     = self._pa.get_device_info_by_index(default_idx)
            host_api = self._pa.get_host_api_info_by_index(info["hostApi"])["name"]
            raw_name = info["name"]
            return AudioDevice(
                index=info["index"],
                name=raw_name,
                display_name=_clean_display_name(raw_name),
                max_input_channels=info["maxInputChannels"],
                default_sample_rate=info["defaultSampleRate"],
                host_api_name=host_api,
                source_type=_classify_source_type(raw_name, False),
                is_loopback=False,
            )
        except Exception as exc:
            logger.error(f"Failed to get default input device: {exc}")
            return None

    def enumerate_output_devices(self) -> list[AudioDevice]:
        """Return all usable audio output devices (speakers/headphones)."""
        devices = []
        try:
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if info.get("maxOutputChannels", 0) <= 0:
                    continue
                host_api = self._pa.get_host_api_info_by_index(info["hostApi"])["name"]
                raw_name = info["name"]
                disp_name = _clean_display_name(raw_name)

                devices.append(AudioDevice(
                    index=info["index"],
                    name=raw_name,
                    display_name=disp_name,
                    max_input_channels=0,
                    default_sample_rate=info["defaultSampleRate"],
                    host_api_name=host_api,
                    source_type="Output Device",
                    is_loopback=False,
                ))
        except Exception as exc:
            logger.error(f"Failed to enumerate output devices: {exc}")
        return devices

    def get_default_output_device(self) -> Optional[AudioDevice]:
        """Return the OS default output device, or None if unavailable."""
        try:
            default_idx = self._pa.get_default_output_device_info()["index"]
            info = self._pa.get_device_info_by_index(default_idx)
            host_api = self._pa.get_host_api_info_by_index(info["hostApi"])["name"]
            raw_name = info["name"]
            return AudioDevice(
                index=info["index"],
                name=raw_name,
                display_name=_clean_display_name(raw_name),
                max_input_channels=0,
                default_sample_rate=info["defaultSampleRate"],
                host_api_name=host_api,
                source_type="Output Device",
                is_loopback=False,
            )
        except Exception as exc:
            logger.error(f"Failed to get default output device: {exc}")
            return None

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_device(
        self,
        device_index: int,
        sample_rate:  int = DEFAULT_SAMPLE_RATE,
        channels:     int = DEFAULT_CHANNELS,
    ) -> bool:
        """
        Return True if the device can open a stream at sample_rate / channels.
        Tries mono first, falls back to stereo (required for WASAPI loopback).
        """
        for ch in (channels, 2):
            try:
                ok = self._pa.is_format_supported(
                    sample_rate,
                    input_device=device_index,
                    input_channels=ch,
                    input_format=pyaudio.paFloat32,
                )
                if ok:
                    return True
            except Exception:
                continue
        logger.warning(
            f"Device {device_index} does not support "
            f"{sample_rate} Hz at mono or stereo"
        )
        return False

    # ── Serial Port Enumeration ───────────────────────────────────────────────

    def enumerate_serial_ports(self) -> list[SerialDeviceInfo]:
        """
        Return all detected serial COM ports.
        Identifies potential ESP32 / USB-UART bridges using vendor IDs
        and driver strings as hints without probing or sending bytes.
        """
        devices = []
        try:
            ports = list_ports.comports()
            for p in ports:
                desc = p.description or ""
                mfg = p.manufacturer or ""
                hwid = p.hwid or ""
                vid = p.vid
                pid = p.pid
                sn = p.serial_number or ""

                # Evaluate ESP32 hint
                is_esp32_hint = False
                if vid in ESP32_HINT_VIDS:
                    is_esp32_hint = True
                elif any("esp32" in s or "espressif" in s for s in (desc.lower(), mfg.lower(), hwid.lower())):
                    is_esp32_hint = True

                # Format clean display name
                clean_desc = desc
                if clean_desc.startswith(p.device):
                    clean_desc = clean_desc[len(p.device):].lstrip(" -")
                
                if not clean_desc or clean_desc == p.device:
                    disp = p.device
                else:
                    disp = f"{p.device} — {clean_desc}"

                if is_esp32_hint:
                    disp = f"{disp} [ESP32 Hint]"

                devices.append(SerialDeviceInfo(
                    port=p.device,
                    description=desc,
                    hwid=hwid,
                    vid=vid,
                    pid=pid,
                    manufacturer=mfg,
                    serial_number=sn,
                    is_esp32_hint=is_esp32_hint,
                    display_name=disp,
                ))
        except Exception as exc:
            logger.error(f"Failed to enumerate serial ports: {exc}")
        return devices
