"""
Unified data export module for RaySID client.

Handles exporting measurement and spectrum data to various formats:
- CSV (with metadata headers)
- JSON (with metadata)
- Graphs (PNG/PDF for spectrum)

All exports include proper scientific metadata:
- ISO 8601 timestamps
- Device information
- Acquisition parameters
"""

import json
import os
from datetime import datetime
from typing import List, Optional, TextIO, TYPE_CHECKING

from .logging_config import get_logger

if TYPE_CHECKING:
    from .models import MeasurementData, SpectrumData, DeviceInfo

logger = get_logger('export')


class MeasurementLogger:
    """Real-time measurement data logger for monitor mode.
    
    Writes measurement data to CSV and/or JSON as it arrives.
    """
    
    def __init__(
        self,
        csv_path: Optional[str] = None,
        json_path: Optional[str] = None,
        device_info: Optional['DeviceInfo'] = None
    ):
        """Initialize the measurement logger.
        
        Args:
            csv_path: Path to CSV file (None to disable CSV)
            json_path: Path to JSON file (None to disable JSON)
            device_info: Device information for metadata
        """
        self.csv_path = csv_path
        self.json_path = json_path
        self.device_info = device_info
        
        self._csv_file: Optional[TextIO] = None
        self._json_measurements: List[dict] = []
        self._measurement_count = 0
        self._start_time = datetime.now()
        
        # Open CSV file and write header
        if csv_path:
            self._csv_file = open(csv_path, 'w', newline='')
            self._write_csv_header()
            logger.info(f"Logging measurements to CSV: {csv_path}")
        
        if json_path:
            logger.info(f"Logging measurements to JSON: {json_path}")
    
    def _write_csv_header(self) -> None:
        """Write CSV file header with metadata."""
        if not self._csv_file:
            return
        
        device_id = self.device_info.device_id if self.device_info else 'Unknown'
        firmware = self.device_info.firmware_version if self.device_info else 'Unknown'
        
        self._csv_file.write(f"# RaySID Radiation Monitor Data Log\n")
        self._csv_file.write(f"# Start Time: {self._start_time.isoformat()}\n")
        self._csv_file.write(f"# Device ID: {device_id}\n")
        self._csv_file.write(f"# Firmware: v{firmware}\n")
        self._csv_file.write(f"#\n")
        self._csv_file.write("datetime_iso,timestamp_unix,cps,cpm,dose_rate_usv_h,temperature_c,battery_percent\n")
        self._csv_file.flush()
    
    def log(self, data: 'MeasurementData', temperature_c: float = 0.0, battery_percent: int = 0) -> None:
        """Log a single measurement.
        
        Args:
            data: Measurement data
            temperature_c: Current device temperature
            battery_percent: Current battery level
        """
        self._measurement_count += 1
        
        # Create ISO timestamp
        dt = datetime.fromtimestamp(data.timestamp)
        iso_timestamp = dt.isoformat(timespec='milliseconds')
        
        # Write to CSV
        if self._csv_file:
            self._csv_file.write(
                f"{iso_timestamp},{data.timestamp:.3f},{data.cps:.4f},{data.cpm:.2f},"
                f"{data.dose_rate_usv:.6f},{temperature_c:.2f},{battery_percent}\n"
            )
            self._csv_file.flush()
        
        # Collect for JSON
        if self.json_path:
            self._json_measurements.append({
                'datetime_iso': iso_timestamp,
                'timestamp_unix': data.timestamp,
                'cps': data.cps,
                'cpm': data.cpm,
                'dose_rate_usv_h': data.dose_rate_usv,
                'temperature_c': temperature_c,
                'battery_percent': battery_percent,
                'temperature_ok': data.temperature_ok,
                'spectrum_full': data.spectrum_full
            })
    
    def close(self) -> None:
        """Close files and write final metadata."""
        end_time = datetime.now()
        duration = (end_time - self._start_time).total_seconds()
        
        # Close CSV with footer
        if self._csv_file:
            self._csv_file.write(f"# End Time: {end_time.isoformat()}\n")
            self._csv_file.write(f"# Duration: {duration:.1f} seconds\n")
            self._csv_file.write(f"# Total Measurements: {self._measurement_count}\n")
            self._csv_file.close()
            self._csv_file = None
            logger.info(f"CSV saved: {self._measurement_count} measurements to {self.csv_path}")
        
        # Write JSON file
        if self.json_path and self._json_measurements:
            device_id = self.device_info.device_id if self.device_info else 'Unknown'
            firmware = self.device_info.firmware_version if self.device_info else 'Unknown'
            
            json_output = {
                'metadata': {
                    'type': 'radiation_monitor_log',
                    'start_time': self._start_time.isoformat(),
                    'end_time': end_time.isoformat(),
                    'duration_seconds': duration,
                    'total_measurements': self._measurement_count,
                    'device_id': device_id,
                    'firmware_version': firmware,
                },
                'measurements': self._json_measurements
            }
            
            with open(self.json_path, 'w') as f:
                json.dump(json_output, f, indent=2)
            logger.info(f"JSON saved: {self._measurement_count} measurements to {self.json_path}")
    
    @property
    def count(self) -> int:
        """Number of measurements logged."""
        return self._measurement_count
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def export_spectrum_csv(
    spectrum: 'SpectrumData',
    path: str,
    include_energy: bool = True
) -> None:
    """Export spectrum data to CSV file.
    
    Args:
        spectrum: Spectrum data to export
        path: Output file path
        include_energy: Include energy (keV) column
    """
    with open(path, 'w') as f:
        # Metadata header
        f.write(f"# RaySID Gamma Spectrum Data\n")
        f.write(f"# Timestamp: {datetime.fromtimestamp(spectrum.timestamp).isoformat()}\n")
        f.write(f"# Device ID: {spectrum.device_id or 'Unknown'}\n")
        f.write(f"# Energy Range: {spectrum.get_energy_range_str()}\n")
        f.write(f"# Total Counts: {spectrum.total_counts}\n")
        f.write(f"# Acquisition Time: {spectrum.acquisition_time:.1f} s\n")
        f.write(f"# Live Time: {spectrum.live_time:.1f} s\n")
        f.write(f"# Real Time: {spectrum.real_time:.1f} s\n")
        f.write(f"# Temperature: {spectrum.temperature:.1f} C\n")
        f.write(f"# Channels: {len(spectrum.channels)}\n")
        f.write(f"#\n")
        
        # Data
        if include_energy:
            f.write("channel,energy_kev,counts\n")
            for i, counts in enumerate(spectrum.channels):
                energy = spectrum.channel_to_kev(i)
                f.write(f"{i},{energy:.2f},{counts}\n")
        else:
            f.write("channel,counts\n")
            for i, counts in enumerate(spectrum.channels):
                f.write(f"{i},{counts}\n")
    
    logger.info(f"Spectrum CSV saved: {path}")


def export_spectrum_json(
    spectrum: 'SpectrumData',
    path: str,
    include_energy_axis: bool = False
) -> None:
    """Export spectrum data to JSON file.
    
    Args:
        spectrum: Spectrum data to export
        path: Output file path
        include_energy_axis: Include pre-calculated energy axis
    """
    output = {
        'metadata': {
            'type': 'gamma_spectrum',
            'timestamp_iso': datetime.fromtimestamp(spectrum.timestamp).isoformat(),
            'timestamp_unix': spectrum.timestamp,
            'device_id': spectrum.device_id or 'Unknown',
            'energy_range': spectrum.get_energy_range_str(),
            'energy_range_code': spectrum.energy_range,
            'total_counts': spectrum.total_counts,
            'acquisition_time_s': spectrum.acquisition_time,
            'live_time_s': spectrum.live_time,
            'real_time_s': spectrum.real_time,
            'temperature_c': spectrum.temperature,
            'num_channels': len(spectrum.channels),
        },
        'channels': spectrum.channels,
    }
    
    if include_energy_axis:
        output['energy_axis_kev'] = spectrum.get_energy_axis()
    
    with open(path, 'w') as f:
        json.dump(output, f, indent=2)
    
    logger.info(f"Spectrum JSON saved: {path}")


def export_spectrum_graph(
    spectrum: 'SpectrumData',
    path: str,
    log_scale: bool = False,
    show_peaks: bool = True,
    title: Optional[str] = None
) -> bool:
    """Export spectrum as graph image.
    
    Args:
        spectrum: Spectrum data to export
        path: Output file path (.png, .pdf, .svg)
        log_scale: Use logarithmic Y axis
        show_peaks: Annotate detected peaks
        title: Custom title
        
    Returns:
        True if successful
    """
    return spectrum.save_graph(path, log_scale=log_scale, show_peaks=show_peaks, title=title)


def save_spectrum(
    spectrum: 'SpectrumData',
    base_name: str,
    save_csv: bool = True,
    save_json: bool = True,
    save_graph: bool = True,
    log_scale: bool = False,
    show_peaks: bool = True,
    title: Optional[str] = None
) -> List[str]:
    """Save spectrum to multiple formats.
    
    Args:
        spectrum: Spectrum data to save
        base_name: Base filename (extensions added automatically)
        save_csv: Save CSV file
        save_json: Save JSON file
        save_graph: Save PNG graph
        log_scale: Use log scale for graph
        show_peaks: Show peaks on graph
        title: Custom graph title
        
    Returns:
        List of saved file paths
    """
    saved_files = []
    
    # Remove extension if present
    if base_name.endswith(('.json', '.csv', '.png', '.pdf', '.svg')):
        base_name = base_name.rsplit('.', 1)[0]
    
    if save_csv:
        csv_path = f"{base_name}.csv"
        export_spectrum_csv(spectrum, csv_path)
        saved_files.append(csv_path)
    
    if save_json:
        json_path = f"{base_name}.json"
        export_spectrum_json(spectrum, json_path)
        saved_files.append(json_path)
    
    if save_graph:
        graph_path = f"{base_name}.png"
        if export_spectrum_graph(spectrum, graph_path, log_scale, show_peaks, title):
            saved_files.append(graph_path)
    
    return saved_files


def generate_filename(prefix: str, extension: str, timestamp: Optional[datetime] = None) -> str:
    """Generate a timestamped filename.
    
    Args:
        prefix: Filename prefix (e.g., 'spectrum', 'monitor')
        extension: File extension (e.g., 'csv', 'json')
        timestamp: Timestamp to use (default: now)
        
    Returns:
        Filename like 'prefix_20241129_125119.ext'
    """
    if timestamp is None:
        timestamp = datetime.now()
    
    ts_str = timestamp.strftime('%Y%m%d_%H%M%S')
    return f"{prefix}_{ts_str}.{extension}"

