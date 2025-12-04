"""
Data models for RaySID client.

Contains dataclasses for measurements, device info, spectrum data, and settings.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional

from .protocol import ENERGY_RANGES, ENERGY_RANGE_NAMES, SENSITIVITY_NAMES
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
    - 8 = 45-3500 keV (full range, ~1.94 keV/channel)
    - 4 = 30-2000 keV (~1.09 keV/channel)
    - 2 = 25-1000 keV (~0.54 keV/channel)
    
    Spectrum Channels (device setting):
    - 1 = 1800 channels (full resolution)
    - 3 = 600 channels (3x binning)
    - 9 = 200 channels (9x binning)
    - 0 = Auto
    
    Energy calibration reference points (8 MeV range):
    - Channel ~30 ≈ 59.5 keV (Am-241)
    - Channel ~340 ≈ 662 keV (Cs-137)
    - Channel ~680 ≈ 1332 keV (Co-60)
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
    
    def channel_to_kev(self, channel: int) -> float:
        """Convert channel number to energy in keV (linear calibration)."""
        min_kev, max_kev = ENERGY_RANGES.get(self.energy_range, (45, 3500))
        num_channels = len(self.channels) if self.channels else 1800
        kev_per_channel = (max_kev - min_kev) / num_channels
        return min_kev + (channel * kev_per_channel)
    
    def kev_to_channel(self, kev: float) -> int:
        """Convert energy in keV to channel number."""
        min_kev, max_kev = ENERGY_RANGES.get(self.energy_range, (45, 3500))
        num_channels = len(self.channels) if self.channels else 1800
        kev_per_channel = (max_kev - min_kev) / num_channels
        return int((kev - min_kev) / kev_per_channel)
    
    def get_energy_range_str(self) -> str:
        """Get human-readable energy range string."""
        return ENERGY_RANGE_NAMES.get(self.energy_range, f"{self.energy_range} MeV")
    
    def subtract(self, background: 'SpectrumData', normalize: bool = True) -> 'SpectrumData':
        """Subtract background spectrum from this spectrum.
        
        Args:
            background: Background spectrum to subtract
            normalize: If True, normalize by acquisition time ratio
            
        Returns:
            New SpectrumData with difference (can be negative - shows true signal vs noise)
        """
        result = SpectrumData()
        result.energy_range = self.energy_range
        result.channels_setting = self.channels_setting
        result.device_id = self.device_id
        result.temperature = self.temperature
        result.timestamp = self.timestamp
        
        # Time normalization factor
        if normalize and background.acquisition_time > 0 and self.acquisition_time > 0:
            time_factor = self.acquisition_time / background.acquisition_time
        else:
            time_factor = 1.0
        
        # Subtract channels (normalize background by time ratio)
        result.channels = []
        for i in range(min(len(self.channels), len(background.channels))):
            diff = self.channels[i] - round(background.channels[i] * time_factor)
            result.channels.append(diff)  # Keep negative values!
        
        # Pad if needed
        while len(result.channels) < len(self.channels):
            result.channels.append(self.channels[len(result.channels)])
        
        # Total counts = sum of positive channels only (net signal)
        result.total_counts = sum(c for c in result.channels if c > 0)
        result.acquisition_time = self.acquisition_time
        result.live_time = self.live_time
        result.real_time = self.real_time
        
        return result
    
    def get_energy_axis(self) -> List[float]:
        """Get energy values for all channels in keV."""
        return [self.channel_to_kev(i) for i in range(len(self.channels))]
    
    def find_peaks(self, threshold: float = 0.1, min_distance: int = 20) -> List[dict]:
        """Find peaks in the spectrum.
        
        Args:
            threshold: Minimum peak height as fraction of max
            min_distance: Minimum distance between peaks in channels
            
        Returns:
            List of dicts with 'channel', 'counts', 'energy_kev'
        """
        peaks = []
        max_counts = max(self.channels) if self.channels else 0
        if max_counts == 0:
            return peaks
        
        min_height = max_counts * threshold
        
        for i in range(min_distance, len(self.channels) - min_distance):
            if self.channels[i] < min_height:
                continue
            
            # Check if this is a local maximum
            is_peak = True
            for j in range(i - min_distance, i + min_distance + 1):
                if j != i and self.channels[j] >= self.channels[i]:
                    is_peak = False
                    break
            
            if is_peak:
                peaks.append({
                    'channel': i,
                    'counts': self.channels[i],
                    'energy_kev': self.channel_to_kev(i)
                })
        
        return sorted(peaks, key=lambda x: x['counts'], reverse=True)
    
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
    
    def save_graph(self, filename: str, log_scale: bool = False,
                   show_peaks: bool = True, title: Optional[str] = None) -> bool:
        """Save spectrum graph as image.
        
        Args:
            filename: Output filename (png, jpg, pdf, svg supported)
            log_scale: Use logarithmic Y axis
            show_peaks: Mark detected peaks
            title: Custom title (default: auto-generated)
            
        Returns:
            True if successful
        """
        try:
            import matplotlib
            matplotlib.use('Agg')  # Non-interactive backend
            import matplotlib.pyplot as plt
        except ImportError:
            logger.error("matplotlib not available. Install with: pip install matplotlib")
            return False
        
        try:
            # Dark modern style
            plt.style.use('dark_background')
            
            # Colors
            bg_color = '#000000'
            panel_color = '#0a0a0a'
            orange = '#ff6b35'
            text_color = '#e0e0e0'
            grid_color = '#333333'
            peak_color = '#00d4ff'
            
            # Check if we have negative values (delta spectrum)
            has_negative = any(c < 0 for c in self.channels)
            is_delta = has_negative
            
            # Create figure
            fig = plt.figure(figsize=(14, 9), facecolor=bg_color)
            ax = fig.add_axes([0.08, 0.15, 0.88, 0.78], facecolor=panel_color)
            
            # Get energy axis
            energies = self.get_energy_axis()
            channel_width = energies[1] - energies[0] if len(energies) > 1 else 1.0
            
            if is_delta:
                # Delta spectrum: positive in orange, negative in cyan
                positive = [max(0, c) for c in self.channels]
                negative = [min(0, c) for c in self.channels]
                
                pos_sum = sum(positive)
                neg_sum = abs(sum(negative))
                net = sum(self.channels)
                
                ax.bar(energies, positive, width=channel_width,
                       color=orange, edgecolor='none', alpha=0.85, align='center')
                ax.bar(energies, negative, width=channel_width,
                       color='#00b4d8', edgecolor='none', alpha=0.85, align='center')
                ax.axhline(y=0, color='#888888', linewidth=1.0, linestyle='-')
                
                # Info panel
                info_line = (f"[+] Emission: +{pos_sum:,}  |  "
                            f"[-] Shielded: -{neg_sum:,}  |  "
                            f"Net: {net:+,}  |  "
                            f"Acq: {self.acquisition_time:.0f}s  |  "
                            f"Temp: {self.temperature:.1f}C  |  "
                            f"{datetime.fromtimestamp(self.timestamp).strftime('%Y-%m-%d %H:%M')}")
                
                fig.text(0.5, 0.05, info_line, ha='center', fontsize=11,
                        color=text_color, family='monospace',
                        bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1a',
                                 edgecolor='#444444', alpha=0.95))
            else:
                # Normal spectrum
                ax.bar(energies, self.channels, width=channel_width,
                       color=orange, edgecolor='none', alpha=0.85, align='center')
                
                info_line = (f"Total: {self.total_counts:,} counts  |  "
                            f"Acq: {self.acquisition_time:.0f}s  |  "
                            f"Temp: {self.temperature:.1f}C  |  "
                            f"Device: {self.device_id or 'Unknown'}  |  "
                            f"{datetime.fromtimestamp(self.timestamp).strftime('%Y-%m-%d %H:%M')}")
                
                fig.text(0.5, 0.05, info_line, ha='center', fontsize=11,
                        color=text_color, family='monospace',
                        bbox=dict(boxstyle='round,pad=0.5', facecolor='#1a1a1a',
                                 edgecolor='#444444', alpha=0.95))
            
            # Set scale
            if log_scale and not is_delta:
                ax.set_yscale('log')
                ax.set_ylim(bottom=0.5)
            
            # Mark peaks
            if show_peaks and not is_delta:
                peaks = self.find_peaks(threshold=0.05, min_distance=30)
                for peak in peaks[:10]:
                    ax.axvline(x=peak['energy_kev'], color=peak_color,
                              linestyle='--', alpha=0.7, linewidth=1.0)
                    ax.annotate(f"{peak['energy_kev']:.0f} keV",
                               xy=(peak['energy_kev'], peak['counts']),
                               xytext=(5, 5), textcoords='offset points',
                               fontsize=9, color=peak_color, fontweight='bold')
            
            # Labels
            ax.set_xlabel('Energy (keV)', fontsize=13, color=text_color, fontweight='medium')
            y_label = 'Counts (Sample - Background)' if is_delta else 'Counts'
            if log_scale and not is_delta:
                y_label += ' (log scale)'
            ax.set_ylabel(y_label, fontsize=13, color=text_color, fontweight='medium')
            
            # Title
            if title is None:
                if self.energy_range in [2, 4, 8]:
                    title = f"RaySID Gamma Spectrum ({self.get_energy_range_str()})"
                else:
                    title = "RaySID Gamma Spectrum"
            ax.set_title(title, fontsize=16, fontweight='bold', color='#ffffff', pad=15)
            
            # Grid
            ax.grid(True, alpha=0.25, which='both', color=grid_color, linestyle='-', linewidth=0.5)
            ax.set_xlim(0, max(energies))
            
            # Style ticks
            ax.tick_params(colors=text_color, labelsize=10)
            for spine in ax.spines.values():
                spine.set_color(grid_color)
                spine.set_linewidth(0.5)
            
            # Save
            plt.savefig(filename, dpi=150, facecolor=bg_color)
            plt.close(fig)
            plt.style.use('default')
            
            logger.info(f"Graph saved to: {filename}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save graph: {e}")
            return False


@dataclass
class DeviceSettings:
    """RaySID settings.
    
    All settings are persisted to ~/.raysid_settings.json (like Android SharedPreferences).
    Device settings are sent to the device on connect and when changed.
    """
    # ===== Client-only settings =====
    clicks_enabled: bool = True        # Enable click sounds on client (Python)
    clicks_scale: int = 20             # Clicks scale 1:N (1, 5, 10, 20, 50, 100, 250)
    spectrum_scale: str = "log"        # "lin" or "log"
    dose_rate_units: str = "uSv/h"     # "uSv/h", "mR/h", "uR/h"
    click_units: str = "CPS"           # "CPS", "CPM"
    
    # ===== Spectrum settings (sent to device) =====
    spectrum_energy_range: int = 8     # Energy range: 2=1000keV, 4=2000keV, 8=3500keV
    update_interval: int = 4           # Sensitivity: 1=fast, 4=normal, 16=accurate, 64=very accurate
    spectrum_channels: int = 1         # Channels: 1=1800, 3=600, 9=200, 0=auto
    
    # ===== Tick/Click settings (sent to device) =====
    ticks_enabled: bool = True         # Enable device ticks (cmd 0x15)
    tick_sound: bool = True            # Device tick sound (cmd 0x03)
    tick_led: bool = True              # Device LED flash on tick (cmd 0x02)
    tick_on_client: bool = False       # Client handles tick sounds (cmd 0x1C)
    tick_duration: int = 2             # Tick sound duration in ms (cmd 0x19)
    led_duration: int = 10             # LED tick duration in 10ms units (cmd 0x32)
    
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
    
    def get_sensitivity_name(self) -> str:
        """Get human-readable sensitivity name."""
        return SENSITIVITY_NAMES.get(self.update_interval, f"Unknown ({self.update_interval})")
    
    def get_alarm1_threshold_str(self) -> str:
        """Get alarm 1 threshold as string."""
        return f"{self.alarm1_threshold} CPS" if self.alarm1_threshold > 0 else "Disabled"
    
    def get_alarm2_threshold_str(self) -> str:
        """Get alarm 2 threshold as string (uSv/h)."""
        if self.alarm2_threshold > 0:
            usv = self.alarm2_threshold / 100.0
            return f"{usv:.2f} uSv/h"
        return "Disabled"
    
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

