"""
RaySID BLE Client Library

A complete Python implementation for communicating with RaySID radiation detector devices.
"""

from .client import RaySIDClient
from .models import (
    MeasurementData,
    DeviceInfo,
    DoseData,
    SpectrumData,
    DeviceSettings,
)
from .protocol import (
    SERVICE_UUID,
    TX_CHAR_UUID,
    RX_CHAR_UUID,
    TAB_SEARCH,
    TAB_SPECTRUM,
    TAB_MAP,
    TAB_LOG,
    TAB_SETTINGS,
    TAB_DEV,
)
from .export import (
    MeasurementLogger,
    save_spectrum,
    export_spectrum_csv,
    export_spectrum_json,
    generate_filename,
)

__version__ = "1.0.0"
__all__ = [
    # Client
    "RaySIDClient",
    # Data models
    "MeasurementData",
    "DeviceInfo",
    "DoseData",
    "SpectrumData",
    "DeviceSettings",
    # Protocol constants
    "SERVICE_UUID",
    "TX_CHAR_UUID",
    "RX_CHAR_UUID",
    "TAB_SEARCH",
    "TAB_SPECTRUM",
    "TAB_MAP",
    "TAB_LOG",
    "TAB_SETTINGS",
    "TAB_DEV",
    # Export utilities
    "MeasurementLogger",
    "save_spectrum",
    "export_spectrum_csv",
    "export_spectrum_json",
    "generate_filename",
]

