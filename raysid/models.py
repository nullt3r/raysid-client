"""
Data models for RaySID client.

Contains dataclasses for measurements, device info, spectrum data, and settings.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List

from . import calibration
from .device_settings import SETTINGS as DEVICE_SETTINGS_REGISTRY
from .logging_config import get_logger

logger = get_logger('models')

# Config file for persistent settings (like Android SharedPreferences)
CONFIG_FILE = os.path.expanduser("~/.raysid_settings.json")


@dataclass
class MeasurementData:
    """Current measurement data."""
    cps: float = 0.0
    cpm: float = 0.0
    dose_rate_usv: float = 0.0
    temperature_ok: bool = True
    spectrum_full: bool = False
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class DeviceInfo:
    """Device information."""
    firmware_version: str = ""
    firmware_subversion: int = 0
    firmware_build: int = 0
    battery_percent: int = 0
    battery_voltage: float = 0.0
    is_charging: bool = False
    temperature_celsius: float = 0.0
    temperature_raw: int = 0
    device_id: str = ""
    # Cs-137 (661.7 keV) peak position channel reported in every status
    # packet; parameterizes the energy calibration (see calibration.py).
    channel_th: int = 0
    channel_th_avg: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class DoseData:
    """Accumulated dose data."""
    dose_count: int = 0
    dose_sum: int = 0
    dose_usv: float = 0.0
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class SpectrumData:
    """Spectrum data (up to 1800 channels).

    RaySID uses variable channels with energy calibration.

    Energy Ranges (device setting):
    - 8 = ~8-1000 keV (narrow range)
    - 4 = ~15-2100 keV
    - 2 = ~20-3500 keV (wide range)

    Spectrum Channels (device setting):
    - 1 = 1800 channels (full resolution)
    - 3 = 600 channels (3x binning)
    - 9 = 200 channels (9x binning)
    - 0 = Auto

    The keV axis is non-linear; see raysid/calibration.py. It is
    parameterized by channel_th_avg (Cs-137 peak position from the
    device status packet).
    """
    channels: List[int] = field(default_factory=lambda: [0] * 1800)
    acquisition_time: float = 0.0
    live_time: float = 0.0
    real_time: float = 0.0
    total_counts: int = 0
    timestamp: float = field(default_factory=time.time)
    energy_range: int = 8  # Device setting: 8, 4, or 2
    channels_setting: int = 1  # Device setting: 1=1800, 3=600, 9=200, 0=auto
    temperature: float = 25.0  # C at acquisition
    device_id: str = ""
    channel_th_avg: float = calibration.DEFAULT_CHANNEL_TH_AVG

    def channel_to_kev(self, channel: int) -> float:
        """Convert channel number to energy in keV."""
        return calibration.channel_to_kev(channel, self.energy_range,
                                          self.channel_th_avg)

    def kev_to_channel(self, kev: float) -> int:
        """Convert energy in keV to channel number."""
        return calibration.kev_to_channel(kev, self.energy_range,
                                          self.channel_th_avg)
    
    def get_energy_range_str(self) -> str:
        """Get human-readable energy range string (same labels as the
        settings registry / the official app)."""
        return DEVICE_SETTINGS_REGISTRY['spectrum_energy_range'].display(self.energy_range)
    
    def get_energy_axis(self) -> List[float]:
        """Get energy values for all channels in keV."""
        return calibration.energy_axis(self.energy_range, self.channel_th_avg,
                                       len(self.channels))
    
    def to_csv(self, include_energy: bool = True) -> str:
        """Export spectrum to CSV string.
        
        For file output, use raysid.export.export_spectrum_csv() instead.
        
        Args:
            include_energy: Include energy (keV) column
            
        Returns:
            CSV formatted string with metadata header
        """
        lines = []
        lines.append(f"# RaySID Gamma Spectrum Data")
        lines.append(f"# Timestamp: {datetime.fromtimestamp(self.timestamp).isoformat()}")
        lines.append(f"# Device ID: {self.device_id or 'Unknown'}")
        lines.append(f"# Energy Range: {self.get_energy_range_str()}")
        lines.append(f"# Total Counts: {self.total_counts}")
        lines.append(f"# Acquisition Time: {self.acquisition_time:.1f} s")
        lines.append(f"# Live Time: {self.live_time:.1f} s")
        lines.append(f"# Real Time: {self.real_time:.1f} s")
        lines.append(f"# Temperature: {self.temperature:.1f} C")
        lines.append(f"# Channels: {len(self.channels)}")
        lines.append(f"#")
        
        if include_energy:
            lines.append("channel,energy_kev,counts")
            for i, counts in enumerate(self.channels):
                energy = self.channel_to_kev(i)
                lines.append(f"{i},{energy:.2f},{counts}")
        else:
            lines.append("channel,counts")
            for i, counts in enumerate(self.channels):
                lines.append(f"{i},{counts}")
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        """Convert to dictionary.
        
        For file output, use raysid.export.export_spectrum_json() instead.
        
        Returns:
            Dictionary with spectrum data and metadata
        """
        return {
            'metadata': {
                'type': 'gamma_spectrum',
                'timestamp_iso': datetime.fromtimestamp(self.timestamp).isoformat(),
                'timestamp_unix': self.timestamp,
                'device_id': self.device_id or 'Unknown',
                'energy_range': self.get_energy_range_str(),
                'energy_range_code': self.energy_range,
                'total_counts': self.total_counts,
                'acquisition_time_s': self.acquisition_time,
                'live_time_s': self.live_time,
                'real_time_s': self.real_time,
                'temperature_c': self.temperature,
                'num_channels': len(self.channels),
            },
            'channels': self.channels,
        }
    

@dataclass
class DeviceSettings:
    """RaySID settings.
    
    All settings are persisted to ~/.raysid_settings.json (like Android SharedPreferences).
    Device settings are sent to the device on connect and when changed.
    """
    # ===== Client-only settings =====
    # (client ticks on/off is the device setting tick_on_client below)
    clicks_scale: int = 20             # Clicks scale 1:N (1, 5, 10, 20, 50, 100, 250)


    # ===== Spectrum settings (sent to device) =====
    spectrum_energy_range: int = 8     # Energy range: 8=25-1000keV, 4=30-2000keV, 2=45-3500keV
    update_interval: int = 4           # Sensitivity: 1=high sens, 4=medium, 16=high accuracy
    spectrum_channels: int = 1         # Channels: 1=1800, 3=600, 9=200, 0=auto

    # ===== Tick/Click settings (sent to device) =====
    ticks_enabled: bool = True         # Enable device ticks (cmd 0x15)
    ticks_scale: int = 20              # Device tick divider 1:N (cmd 0x16)
    tick_sound: bool = True            # Device tick sound (cmd 0x03)
    tick_led: bool = True              # Device LED flash on tick (cmd 0x02)
    tick_on_client: bool = False       # Client handles tick sounds (cmd 0x1C)
    tick_duration: int = 2             # Tick sound type code 1-5,11-15 (cmd 0x19)
    led_duration: int = 10             # LED tick length in 10ms units (cmd 0x32)
    
    # ===== Alarm Level 1 - CPS (sent to device) =====
    alarm1_enabled: bool = False       # Enable CPS alarm (cmd 0x17)
    alarm1_threshold: int = 0          # CPS threshold (cmd 0x13)
    alarm1_led: bool = True            # Alarm 1 LED flash (cmd 0x05)
    alarm1_sound: bool = True          # Alarm 1 device sound (cmd 0x06)
    alarm1_vibro: bool = True          # Alarm 1 vibration (cmd 0x07)
    alarm1_on_client: bool = False     # Alarm 1 sound on client (cmd 0x3B)
    
    # ===== Alarm Level 2 - Dose Rate (sent to device) =====
    alarm2_enabled: bool = False       # Enable dose rate alarm (cmd 0x18)
    alarm2_threshold: int = 0          # Dose threshold in uSv/h × 100 (cmd 0x14)
    alarm2_led: bool = True            # Alarm 2 LED flash (cmd 0x08)
    alarm2_sound: bool = True          # Alarm 2 device sound (cmd 0x09)
    alarm2_vibro: bool = True          # Alarm 2 vibration (cmd 0x0A)
    alarm2_on_client: bool = False     # Alarm 2 sound on client (cmd 0x3C)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)
    
    def save_to_file(self) -> None:
        """Save settings to config file (like Android SharedPreferences)."""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.to_dict(), f, indent=2)
            logger.debug(f"Settings saved to {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Could not save settings: {e}")
    
    @classmethod
    def load_from_file(cls) -> 'DeviceSettings':
        """Load settings from config file."""
        settings = cls()
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    for key, value in data.items():
                        if hasattr(settings, key):
                            setattr(settings, key, value)
                logger.debug(f"Settings loaded from {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Could not load settings: {e}")
        return settings

