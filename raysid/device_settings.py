"""
Single source of truth for RaySID device settings.

Every setting the device accepts is described once here: which
attribute it maps to on DeviceSettings (and in the settings file),
the human label, the valid values with their app-identical labels,
and how it is encoded on the wire.

Consumers:
- RaySIDClient.apply_setting()      — validate, send, persist (the only
  code path that changes a device setting)
- RaySIDClient._apply_stored_settings() — replay everything on connect
- raysid.tui.SettingsScreen         — interactive menu
- cli cmd_settings                  — flags and --show listing

Choice lists are taken verbatim from the official app's resource arrays
(arrays.xml, v1.3.1e); command bytes verified against Main.java
onSharedPreferenceChanged(). Sensitivity 64 is a documented legacy
device value the app UI no longer offers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .protocol import (
    CMD_SET_UPDATE_INTERVAL,
    CMD_SET_ENERGY_RANGE,
    CMD_SET_SPECTRUM_CHANNELS,
    CMD_SET_TICKS_ENABLED,
    CMD_SET_TICKS_SCALE,
    CMD_SET_TICK_SOUND,
    CMD_SET_TICK_LED,
    CMD_SET_TICK_ON_CLIENT,
    CMD_SET_TICK_DURATION,
    CMD_SET_LED_DURATION,
    CMD_SET_ALARM1_ENABLED,
    CMD_SET_ALARM1_THRESHOLD,
    CMD_SET_ALARM1_LED,
    CMD_SET_ALARM1_SOUND,
    CMD_SET_ALARM1_VIBRO,
    CMD_SET_ALARM1_ON_CLIENT,
    CMD_SET_ALARM2_ENABLED,
    CMD_SET_ALARM2_THRESHOLD,
    CMD_SET_ALARM2_LED,
    CMD_SET_ALARM2_SOUND,
    CMD_SET_ALARM2_VIBRO,
    CMD_SET_ALARM2_ON_CLIENT,
)

_BOOL = ((False, "off"), (True, "on"))


@dataclass(frozen=True)
class SettingDef:
    """One device setting: identity, valid values, wire format."""

    key: str          # attribute on DeviceSettings / settings-file key
    label: str
    command: int      # command byte sent to the device
    encoding: str     # 'bool' | 'byte' | 'word'
    choices: Tuple[Tuple[object, str], ...]  # (value, display label)

    def values(self) -> tuple:
        return tuple(v for v, _ in self.choices)

    def is_valid(self, value) -> bool:
        return value in self.values()

    def display(self, value) -> str:
        for v, name in self.choices:
            if v == value:
                return name
        return str(value)

    def encode(self, value) -> bytes:
        if self.encoding == "bool":
            return bytes([1 if value else 0])
        if self.encoding == "byte":
            return bytes([value & 0xFF])
        # word, big-endian — matches the app's {cmd, v/256, v%256}
        return bytes([(value >> 8) & 0xFF, value & 0xFF])


SETTING_GROUPS: Tuple[Tuple[str, Tuple[SettingDef, ...]], ...] = (
    ("MEASUREMENT", (
        SettingDef("update_interval", "Sensitivity", CMD_SET_UPDATE_INTERVAL, "byte", (
            (1, "High sensitivity / low accuracy"),
            (4, "Medium sensitivity / medium accuracy"),
            (16, "Low sensitivity / high accuracy"),
            (64, "Very accurate (legacy)"),
        )),
    )),
    ("SPECTRUM", (
        SettingDef("spectrum_energy_range", "Energy range", CMD_SET_ENERGY_RANGE, "byte", (
            (8, "25-1000 keV"),
            (4, "30-2000 keV"),
            (2, "45-3500 keV"),
        )),
        SettingDef("spectrum_channels", "Channels", CMD_SET_SPECTRUM_CHANNELS, "byte", (
            (1, "1800"), (3, "600"), (9, "200"), (0, "Auto"),
        )),
    )),
    ("TICKS", (
        SettingDef("ticks_enabled", "Ticks", CMD_SET_TICKS_ENABLED, "bool", _BOOL),
        SettingDef("ticks_scale", "Tick scale", CMD_SET_TICKS_SCALE, "byte", (
            (1, "1:1"), (5, "1:5"), (10, "1:10"), (20, "1:20"),
            (50, "1:50"), (100, "1:100"), (250, "1:250"),
        )),
        SettingDef("tick_sound", "Device sound", CMD_SET_TICK_SOUND, "bool", _BOOL),
        SettingDef("tick_duration", "Sound type", CMD_SET_TICK_DURATION, "word", (
            (1, "Type 1"), (2, "Type 2"), (3, "Type 3"), (4, "Type 4"),
            (5, "Type 5"), (11, "Type 6"), (12, "Type 7"), (13, "Type 8"),
            (14, "Type 9"), (15, "Type 10"),
        )),
        SettingDef("tick_led", "LED flash", CMD_SET_TICK_LED, "bool", _BOOL),
        SettingDef("led_duration", "LED length", CMD_SET_LED_DURATION, "word", (
            (1, "10 ms"), (2, "20 ms"), (3, "30 ms"),
            (5, "50 ms"), (10, "100 ms"), (20, "200 ms"),
        )),
        SettingDef("tick_on_client", "Sound on client", CMD_SET_TICK_ON_CLIENT, "bool", _BOOL),
    )),
    ("ALARM 1 · COUNT RATE", (
        SettingDef("alarm1_enabled", "Alarm", CMD_SET_ALARM1_ENABLED, "bool", _BOOL),
        SettingDef("alarm1_threshold", "Threshold", CMD_SET_ALARM1_THRESHOLD, "word", (
            (0, "Dynamic"), (5, "5 CPS"), (10, "10 CPS"), (20, "20 CPS"),
            (30, "30 CPS"), (50, "50 CPS"), (100, "100 CPS"),
            (200, "200 CPS"), (500, "500 CPS"), (1000, "1000 CPS"),
            (2000, "2000 CPS"), (5000, "5000 CPS"),
        )),
        SettingDef("alarm1_led", "Device LED", CMD_SET_ALARM1_LED, "bool", _BOOL),
        SettingDef("alarm1_sound", "Device sound", CMD_SET_ALARM1_SOUND, "bool", _BOOL),
        SettingDef("alarm1_vibro", "Vibration", CMD_SET_ALARM1_VIBRO, "bool", _BOOL),
        SettingDef("alarm1_on_client", "Ticks during alarm", CMD_SET_ALARM1_ON_CLIENT, "bool", _BOOL),
    )),
    ("ALARM 2 · DOSE RATE", (
        SettingDef("alarm2_enabled", "Alarm", CMD_SET_ALARM2_ENABLED, "bool", _BOOL),
        SettingDef("alarm2_threshold", "Threshold", CMD_SET_ALARM2_THRESHOLD, "word", (
            (0, "Dynamic"), (20, "0.20 µSv/h"), (30, "0.30 µSv/h"),
            (50, "0.50 µSv/h"), (100, "1.00 µSv/h"), (200, "2.00 µSv/h"),
            (500, "5.00 µSv/h"), (1000, "10.00 µSv/h"),
            (2000, "20.00 µSv/h"), (5000, "50.00 µSv/h"),
        )),
        SettingDef("alarm2_led", "Device LED", CMD_SET_ALARM2_LED, "bool", _BOOL),
        SettingDef("alarm2_sound", "Device sound", CMD_SET_ALARM2_SOUND, "bool", _BOOL),
        SettingDef("alarm2_vibro", "Vibration", CMD_SET_ALARM2_VIBRO, "bool", _BOOL),
        SettingDef("alarm2_on_client", "Ticks during alarm", CMD_SET_ALARM2_ON_CLIENT, "bool", _BOOL),
    )),
)

# Flat lookups
SETTINGS = {d.key: d for _, group in SETTING_GROUPS for d in group}
ALL_SETTINGS = tuple(d for _, group in SETTING_GROUPS for d in group)
