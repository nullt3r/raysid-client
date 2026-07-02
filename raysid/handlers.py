"""
Packet handlers for RaySID BLE protocol.

Contains functions for parsing and handling different packet types
received from the RaySID device.
"""

import struct
import time
from typing import Dict, TYPE_CHECKING

from .protocol import (
    PACKET_TYPE_SPECTRUM_STATS,
    PACKET_TYPE_STATUS,
    PACKET_TYPE_SETTINGS_EXT,
    PACKET_TYPE_VERSION_RESP,
    PACKET_TYPE_MEASUREMENT,
    PACKET_TYPE_DOSE_SYNC,
    PACKET_TYPE_VERSION,
    PACKET_TYPE_STATUS_EXT,
    PACKET_TYPE_CALIB_RESP,
    PACKET_TYPE_LIMITS_RESP,
    PACKET_TYPE_SPECTRUM_0,
    PACKET_TYPE_SPECTRUM_1,
    PACKET_TYPE_SPECTRUM_2,
    PACKET_TYPE_CALIBRATION,
    PACKET_TYPE_BATTERY,
    PACKET_TYPE_SETTINGS_SYNC,
)
from .logging_config import get_protocol_logger

if TYPE_CHECKING:
    from .client import RaySIDClient

logger = get_protocol_logger()


def two_bytes_to_int(b1: int, b2: int) -> int:
    """Convert two bytes to int (big-endian)."""
    return ((b1 & 0xFF) * 256) + (b2 & 0xFF)


def three_bytes_to_int(high: int, mid: int, low: int) -> int:
    """Convert 3 bytes to 24-bit int (high*65536 + mid*256 + low)."""
    return ((high & 0xFF) << 16) | ((mid & 0xFF) << 8) | (low & 0xFF)


def unpack_value(value: int) -> int:
    """Unpack encoded value (exponential format)."""
    exponent = value // 6000
    mantissa = value % 6000
    return mantissa * (10 ** exponent)


def calculate_crc32(data: bytes) -> int:
    """Firmware packet checksum (name kept from BLEservice.java).

    Despite the name this is NOT a real CRC32 — it sums the data as
    little-endian 32-bit words. Must stay bit-identical to the firmware.
    """
    crc = 0
    for i in range(0, len(data), 4):
        remaining = len(data) - i
        if remaining >= 4:
            value = struct.unpack('<I', data[i:i+4])[0]
        elif remaining == 3:
            value = (data[i+2] << 16) | (data[i+1] << 8) | data[i]
        elif remaining == 2:
            value = (data[i+1] << 8) | data[i]
        else:
            value = data[i]
        crc = (crc + value) & 0xFFFFFFFF
    return crc


def checksum2(data: bytes) -> int:
    """XOR checksum."""
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum & 0xFF


def checksum3(data, length: int, offset: int = 0) -> int:
    """3-byte XOR checksum used for packet validation.

    From BLEservice.java checksum3():
    XORs `length` bytes starting at `offset` in 3-byte triplets,
    returning 24-bit result. Accepts bytes or bytearray so callers can
    checksum in place without copying.
    """
    result = 0
    end = offset + length
    for i in range(offset, end, 3):
        b0 = data[i] & 0xFF
        b1 = data[i + 1] & 0xFF if i + 1 < end else 0
        b2 = data[i + 2] & 0xFF if i + 2 < end else 0
        result ^= (b0 << 16) | (b1 << 8) | b2
    return result & 0xFFFFFF


def create_packet(command: int, data: bytes = b'') -> bytes:
    """Create packet with CRC32 format (big-endian CRC)."""
    command_data = bytes([command]) + data
    crc = calculate_crc32(command_data)
    crc_bytes = struct.pack('>I', crc)  # Big-endian!
    
    inner_packet = bytes([0xEE]) + crc_bytes + command_data
    xor_checksum = checksum2(inner_packet)
    total_length = len(inner_packet) + 3
    
    return bytes([0xFF, xor_checksum]) + inner_packet + bytes([total_length])


def parse_packet(packet: bytes) -> Dict:
    """Parse validated packet (checksum already verified).
    
    Format: [Length N][Type][Data...][Checksum 3 bytes]
    Length byte = total packet size (N bytes)
    Data = packet[2:N-3] (between type and checksum)
    """
    if len(packet) < 5:  # Minimum: len + type + 3 checksum (no data)
        return {"valid": False, "error": "Too short"}

    packet_type = packet[1]

    if packet_type in (0x30, 0x31, 0x32):
        # Spectrum packets: handler needs the raw packet (minus checksum)
        data = packet[:-3]
    else:
        # Regular packets: data is from byte 2 to N-3
        data = packet[2:-3]
    
    return {
        "valid": True,
        "type": packet_type,
        "data": data,
        "raw_packet": packet
    }


def handle_status(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x02 - Status + Temperature + Battery.
    
    Temperature formula: (raw / 10.0) - 100.0 = C
    """
    if len(data) >= 6:
        # Temperature (bytes 0-1, little-endian)
        temp_raw = two_bytes_to_int(data[1], data[0])
        temp_celsius = (temp_raw / 10.0) - 100.0
        client.device_info.temperature_raw = temp_raw
        client.device_info.temperature_celsius = temp_celsius
        
        # Battery (bytes 2-3, little-endian)
        battery = two_bytes_to_int(data[3], data[2])
        client.device_info.battery_percent = battery
        
        # Charging status (byte 4)
        if len(data) > 4:
            client.device_info.is_charging = (data[4] & 0xFF) > 0
        
        # Cs-137 peak position channel (bytes 5-8): instantaneous and
        # running average, both signed with +700 offset. The average
        # parameterizes the energy calibration (see calibration.py).
        # Java: channelTh = twoBytesToShort(bArr[8], bArr[7]) + 700;
        #       channelThAvg = twoBytesToShort(bArr[10], bArr[9]) / 100 + 700
        if len(data) >= 9:
            client.device_info.channel_th = (
                int.from_bytes(bytes([data[6], data[5]]), 'big', signed=True) + 700
            )
            client.device_info.channel_th_avg = (
                int.from_bytes(bytes([data[8], data[7]]), 'big', signed=True) / 100.0 + 700.0
            )

        # Status flags (byte 9)
        if len(data) > 9:
            flags = data[9] & 0xFF
            client.measurement.temperature_ok = (flags % 2) == 1
            client.measurement.spectrum_full = ((flags // 2) % 2) == 1

        logger.debug(f"Status: Temp={temp_celsius:.1f}C, Battery={battery}%, Charging={client.device_info.is_charging}")


def handle_measurement(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x17 - Live CPS/Energy."""
    if len(data) < 6:
        return
    
    cps = -1.0
    energy = -1.0
    
    num_triplets = len(data) // 3

    for i in range(num_triplets):
        idx = i * 3
        range_id = data[idx] & 0xFF
        value_low = data[idx + 1] & 0xFF
        value_high = data[idx + 2] & 0xFF
        
        raw_value = two_bytes_to_int(value_high, value_low)
        unpacked = unpack_value(raw_value)
        
        if range_id == 0:  # CPS
            cps = unpacked / 600.0
        elif range_id == 1:  # Energy (dose rate)
            energy = unpacked / 600.0 / 100.0
    
    if cps >= 0:
        client.measurement.cps = cps
        client.measurement.cpm = cps * 60.0
        client.measurement.timestamp = time.time()
        
        # Generate tick sounds based on CPS
        if client.tick_generator:
            client.tick_generator.update(cps)
    
    if energy >= 0:
        client.measurement.dose_rate_usv = energy
    
    # Call callback
    if client.on_measurement:
        client.on_measurement(client.measurement)


def handle_dose_sync(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x06 - Dose sync.
    
    From Main.java line 4081:
    - Packed sum: twoBytesToInt(bArr2[3], bArr2[2]) = byte3*256 + byte2 (little-endian in memory)
    - Count: eightBytesToLong(bArr2[11]...bArr2[4]) = byte11 highest (little-endian in memory)
    - Sum needs unpackValue() to decode exponential format
    """
    if len(data) >= 10:
        # Packed sum: 2 bytes at data[0:2], little-endian, needs unpackValue
        # Java: twoBytesToInt(bArr2[3], bArr2[2]) = bArr2[3]*256 + bArr2[2]
        packed_sum = int.from_bytes(data[0:2], 'little')
        dose_sum = unpack_value(packed_sum)
        
        # Count: 8 bytes at data[2:10], little-endian  
        # Java: eightBytesToLong(bArr2[11]..bArr2[4]) = bArr2[11] highest, bArr2[4] lowest
        dose_cnt = int.from_bytes(data[2:10], 'little')
        
        client.dose_data.dose_count = dose_cnt
        client.dose_data.dose_sum = dose_sum
        # Convert sum to uSv: sum / 600 / 100
        client.dose_data.dose_usv = dose_sum / 600.0 / 100.0
        client.dose_data.timestamp = time.time()
        
        logger.debug(f"Dose: Count={dose_cnt}, Sum={dose_sum}, Total={client.dose_data.dose_usv:.6f} uSv")
        
        if client.on_dose:
            client.on_dose(client.dose_data)


def handle_version(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x1F - Firmware version."""
    # Only parse version once
    if client.device_info.firmware_version:
        return
    
    if len(data) >= 4:
        version = data[0] & 0xFF
        subversion = data[1] & 0xFF
        build = ((data[2] & 0xFF) * 256) + (data[3] & 0xFF)
        
        # Sanity check
        if version > 10 or subversion > 99:
            logger.debug(f"Ignoring invalid version: {version}.{subversion}.{build}")
            return
        
        client.device_info.firmware_version = f"{version}.{subversion}.{build}"
        client.device_info.firmware_subversion = subversion
        client.device_info.firmware_build = build
        
        # Extract device ID from bytes 4-7 if present
        if len(data) >= 8:
            device_id_bytes = data[4:8]
            client.device_info.device_id = device_id_bytes.hex().upper()
        
        # Don't log here - let the client do it after receiving


def handle_spectrum(client: 'RaySIDClient', bArr2: bytes, packet_type: int = None) -> None:
    """Handle Type 0x30/0x31/0x32 - Spectrum data (delta-encoded).
    
    Based on Main.java parseValidPacket() lines 3553-3700.
    
    Delta encoding types (control byte / 64):
    - 0: 4-bit deltas (2 per byte, signed nibbles)
    - 1: 8-bit deltas (signed bytes)
    - 2: 12-bit deltas (signed, packed)
    - 3: 16-bit deltas (signed)
    - 4: 24-bit delta (when control == 0)
    """
    if len(bArr2) < 7:
        logger.debug(f"Spectrum packet too short: {len(bArr2)} bytes")
        return
    
    if not client.spectrum_acquiring:
        logger.debug(f"Spectrum packet ignored (not acquiring), type=0x{bArr2[1]:02X}")
        return
    
    logger.debug(f"handle_spectrum: Processing {len(bArr2)} bytes, type=0x{bArr2[1]:02X}")
    
    # Determine channelsDiv from packet type
    pkt_type = bArr2[1]
    channels_div = 1
    if pkt_type == 0x31:
        channels_div = 3
    elif pkt_type == 0x32:
        channels_div = 9

    channels = client.spectrum_data.channels
    received = client._spectrum_channel_received
    n_channels = len(channels)
    n_received = len(received)

    def store(channel: int, value: int) -> int:
        """Write value into `channels_div` consecutive channels; return next channel."""
        v = max(0, value // channels_div)
        for _ in range(channels_div):
            if channel < n_channels:
                channels[channel] = v
                if channel < n_received:
                    received[channel] = True
                channel += 1
        return channel

    # Parse starting channel and first absolute value
    channel = two_bytes_to_int(bArr2[3], bArr2[2])
    value = ((bArr2[6] & 0xFF) << 16) | ((bArr2[5] & 0xFF) << 8) | (bArr2[4] & 0xFF)

    start_channel = channel
    channel = store(channel, value)

    # Parse delta-encoded values. Bound verified against decompiled
    # Main.java v1.3.1e: Java scans `i < length - 4` on the full packet
    # (checksum attached); bArr2 here is the packet minus its 3 checksum
    # bytes, so len - 1 is the identical bound.
    i = 7
    data_end = len(bArr2) - 1

    while i < data_end:
        control = bArr2[i] & 0xFF
        encoding_type = control // 64
        count = control % 64

        if control == 0:
            encoding_type = 4
            count = 1

        i += 1

        if encoding_type == 0:
            # 4-bit deltas (2 per byte, signed nibbles)
            j = 0
            while j < count:
                if i >= len(bArr2):
                    break
                byte_val = bArr2[i] & 0xFF

                delta = byte_val // 16
                if delta > 7:
                    delta -= 16
                value += delta
                channel = store(channel, value)
                j += 1

                if j < count:
                    delta = byte_val % 16
                    if delta > 7:
                        delta -= 16
                    value += delta
                    channel = store(channel, value)
                    j += 1

                i += 1

        elif encoding_type == 1:
            # 8-bit deltas (signed bytes)
            for j in range(count):
                if i >= len(bArr2):
                    break
                delta = bArr2[i] & 0xFF
                if delta > 127:
                    delta -= 256
                value += delta
                channel = store(channel, value)
                i += 1

        elif encoding_type == 2:
            # 12-bit deltas (signed, packed 2 per 3 bytes)
            j = 0
            while j < count:
                if i + 1 >= len(bArr2):
                    break
                delta = ((bArr2[i] & 0xFF) << 4) | ((bArr2[i+1] >> 4) & 0x0F)
                delta &= 0xFFF
                if delta > 2047:
                    delta -= 4096
                value += delta
                channel = store(channel, value)
                j += 1
                i += 2

                if j < count:
                    if i >= len(bArr2):
                        break
                    delta = ((bArr2[i-1] & 0x0F) << 8) | (bArr2[i] & 0xFF)
                    if delta > 2047:
                        delta -= 4096
                    value += delta
                    channel = store(channel, value)
                    j += 1
                    i += 1

        elif encoding_type == 3:
            # 16-bit deltas (signed)
            for j in range(count):
                if i + 1 >= len(bArr2):
                    break
                delta = ((bArr2[i+1] & 0xFF) << 8) + (bArr2[i] & 0xFF)
                if delta > 32767:
                    delta -= 65536
                value += delta
                channel = store(channel, value)
                i += 2

        elif encoding_type == 4:
            # 24-bit delta (signed)
            if i + 2 >= len(bArr2):
                break
            delta = ((bArr2[i+2] & 0xFF) << 16) | ((bArr2[i+1] & 0xFF) << 8) | (bArr2[i] & 0xFF)
            if delta > 8388607:
                delta -= 16777216
            value += delta
            channel = store(channel, value)
            i += 3
    
    if channel % 200 == 0 or channel >= 1790:
        logger.debug(f"Spectrum progress: ch {start_channel}-{channel}")


def handle_calibration(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x57 - Calibration data."""
    logger.debug(f"Calibration: {len(data)} bytes")


def handle_battery(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x5C - Battery status."""
    if len(data) >= 2:
        client.device_info.battery_percent = data[0]
        client.device_info.battery_voltage = data[1] / 10.0
        logger.debug(f"Battery: {client.device_info.battery_percent}%")


def handle_status_ext(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x20 - Extended status (very frequent)."""
    pass  # Silently ignore


def handle_settings_ext(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x0A - Extended settings."""
    logger.debug(f"Settings ext: {len(data)} bytes")


def handle_limits_resp(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x2B - Spectrum limits response."""
    logger.debug(f"Limits: {len(data)} bytes")


def handle_settings_sync(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x1B - Settings sync response."""
    if len(data) < 4:
        return
    
    logger.debug(f"Settings sync: {len(data)} bytes")
    
    # Energy range at byte[18] in original packet = data[16]
    if len(data) >= 17:
        energy_range = data[16] & 0xFF
        if energy_range in [2, 4, 8]:
            client.settings.spectrum_energy_range = energy_range
            logger.debug(f"Energy range={energy_range}")
    
    # Update interval at byte[23] in original packet = data[21]
    if len(data) >= 22:
        update_interval = data[21] & 0xFF
        if update_interval in [1, 4, 16, 64]:
            client.settings.update_interval = update_interval
            logger.debug(f"Update interval={update_interval}")


def handle_spectrum_stats(client: 'RaySIDClient', data: bytes) -> None:
    """Handle Type 0x01 - Spectrum Statistics."""
    if len(data) < 15:
        logger.debug(f"Spectrum stats packet too short: {len(data)} bytes")
        return
    
    # Parse spectrum statistics
    total_counts = int.from_bytes(data[0:4], 'little')
    real_time_raw = int.from_bytes(data[4:8], 'little')
    real_time = real_time_raw / 12.0
    energy_raw = int.from_bytes(data[8:12], 'little')
    energy = energy_raw / 600.0
    high_energy_counts = int.from_bytes(data[12:15], 'little')
    
    if client.spectrum_acquiring:
        if total_counts > 0:
            client.spectrum_data.total_counts = total_counts
            client.spectrum_data.real_time = real_time
            client.spectrum_data.live_time = real_time
            client.spectrum_data.acquisition_time = real_time
            client._spectrum_stats_received = True
    
    logger.debug(f"Spectrum stats: Counts={total_counts:,}, Time={real_time:.1f}s")


# Handler mapping
PACKET_HANDLERS = {
    PACKET_TYPE_SPECTRUM_STATS: handle_spectrum_stats,
    PACKET_TYPE_STATUS: handle_status,
    PACKET_TYPE_SETTINGS_EXT: handle_settings_ext,
    PACKET_TYPE_VERSION_RESP: handle_version,
    PACKET_TYPE_MEASUREMENT: handle_measurement,
    PACKET_TYPE_DOSE_SYNC: handle_dose_sync,
    PACKET_TYPE_VERSION: handle_version,
    PACKET_TYPE_STATUS_EXT: handle_status_ext,
    PACKET_TYPE_CALIB_RESP: handle_calibration,
    PACKET_TYPE_LIMITS_RESP: handle_limits_resp,
    PACKET_TYPE_SPECTRUM_0: handle_spectrum,
    PACKET_TYPE_SPECTRUM_1: handle_spectrum,
    PACKET_TYPE_SPECTRUM_2: handle_spectrum,
    PACKET_TYPE_CALIBRATION: handle_calibration,
    PACKET_TYPE_BATTERY: handle_battery,
    PACKET_TYPE_SETTINGS_SYNC: handle_settings_sync,
}


def handle_packet(client: 'RaySIDClient', packet: Dict) -> None:
    """Handle parsed packet by dispatching to appropriate handler."""
    packet_type = packet["type"]
    data = packet["data"]
    
    # Debug log for spectrum-related packets
    if packet_type in [0x30, 0x31, 0x32, 0x01]:
        logger.debug(f"Spectrum-related packet: type=0x{packet_type:02X}, len={len(data)}")
    
    handler = PACKET_HANDLERS.get(packet_type)
    if handler:
        handler(client, data)
    else:
        logger.debug(f"Unknown packet type 0x{packet_type:02X}: {data.hex()}")

