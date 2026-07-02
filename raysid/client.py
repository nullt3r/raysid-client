"""
RaySID BLE Client - Main client class.

Provides the core functionality for communicating with RaySID radiation detector devices
over Bluetooth Low Energy (BLE).
"""

import asyncio
import time
from typing import Optional, Callable

from bleak import BleakClient, BleakScanner

from .protocol import (
    SERVICE_UUID,
    TX_CHAR_UUID,
    RX_CHAR_UUID,
    CMD_SET_MODE,
    CMD_PING,
    CMD_SETTINGS_SYNC,
    CMD_VERSION_REQUEST,
    CMD_ENABLE_MEASUREMENT,
    CMD_DOSE_RESET,
    CMD_SPECTRUM_REQUEST,
    CMD_CLEAR_SPECTRUM,
    TAB_SEARCH,
    TAB_SPECTRUM,
)
from .device_settings import ALL_SETTINGS, SETTINGS
from .models import (
    MeasurementData,
    DeviceInfo,
    DoseData,
    SpectrumData,
    DeviceSettings,
)
from .audio import TickSoundGenerator, is_audio_available
from .handlers import (
    create_packet,
    parse_packet,
    handle_packet,
    checksum3,
    three_bytes_to_int,
)
from .logging_config import get_client_logger, get_ble_logger

logger = get_client_logger()
ble_logger = get_ble_logger()


class RaySIDClient:
    """Complete RaySID BLE Client.
    
    Provides methods for:
    - Connecting to RaySID devices via BLE
    - Reading live radiation measurements (CPS, CPM, uSv/h)
    - Acquiring spectrum data
    - Managing device settings
    - Handling dose accumulation
    """
    
    def __init__(
        self,
        verbose: bool = False,
        enable_sound: bool = False,
        debug_packets: bool = False,
        auto_reconnect: bool = True,
        retry_delay: float = 2.0,
        max_reconnect_attempts: 'Optional[int]' = None
    ):
        """Initialize the RaySID client.
        
        Args:
            verbose: Enable verbose logging
            enable_sound: Enable Geiger counter tick sounds
            debug_packets: Enable detailed packet tracing
            auto_reconnect: Automatically reconnect on connection loss
            retry_delay: Delay between reconnection attempts in seconds
            max_reconnect_attempts: give up after this many reconnect
                attempts (None = keep trying forever)
        """
        self.client: Optional[BleakClient] = None
        self.target_address: Optional[str] = None
        self.verbose = verbose
        self.debug_packets = debug_packets
        
        # Auto-reconnect settings
        self.auto_reconnect = auto_reconnect
        self.retry_delay = retry_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self._reconnecting = False
        self._intentional_disconnect = False
        
        # Packet processing
        self.rx_buffer = bytearray()
        self.packet_delimiter = 0x72  # 'r' - delimiter byte between packets
        
        # RX statistics for packet loss diagnostics
        self._stats_rx_bytes = 0
        self._stats_rx_notifications = 0
        self._stats_packets_valid = 0
        self._stats_bytes_discarded = 0
        self._stats_delimiters_consumed = 0
        
        # State
        self.connected = False
        self.measurement_active = False
        self.active_tab = TAB_SEARCH
        # Keepalive task — must hold a reference: asyncio only keeps weak
        # references to tasks, so a bare create_task() can be GC'd mid-run.
        self._ping_task: Optional[asyncio.Task] = None
        
        # Data
        self.measurement = MeasurementData()
        self.device_info = DeviceInfo()
        self.dose_data = DoseData()
        self.spectrum_data = SpectrumData()
        self.settings = DeviceSettings.load_from_file()
        
        # Callbacks
        self.on_measurement: Optional[Callable[[MeasurementData], None]] = None
        self.on_dose: Optional[Callable[[DoseData], None]] = None
        self.on_packet: Optional[Callable[[int, int], None]] = None  # (type, length)
        
        # Spectrum acquisition state
        self.spectrum_acquiring = False
        self._spectrum_stats_received = False
        self._spectrum_channel_received = [False] * 1800
        
        # Sound generator
        self.tick_generator: Optional[TickSoundGenerator] = None
        if enable_sound:
            if is_audio_available():
                self.tick_generator = TickSoundGenerator()
            else:
                logger.warning("Audio requested but numpy/sounddevice not available")
    
    # ==================== PACKET PROCESSING ====================
    
    def _notification_handler(self, sender, data: bytearray) -> None:
        """BLE notification handler.
        
        NOTE: This runs synchronously in the BLE callback context.
        Keep processing fast to avoid missing notifications.
        """
        self._stats_rx_bytes += len(data)
        self._stats_rx_notifications += 1
        ble_logger.debug(f"RX NOTIFY: {len(data)} bytes, acquiring={self.spectrum_acquiring}")
        self.rx_buffer.extend(data)
        self._process_rx_buffer()
    
    def _process_rx_buffer(self) -> None:
        """Process received data buffer with checksum validation.

        Uses the same algorithm as Java BLEservice.onCharacteristicChanged():
        - Skip leading delimiter bytes (0x72 = 'r')
        - Search for valid packet starting at each byte position
        - Validate checksum before consuming packet
        - Track bytes discarded for diagnostic purposes
        """
        max_iterations = 100
        iterations = 0
        buf = self.rx_buffer

        while len(buf) >= 4 and iterations < max_iterations:
            iterations += 1

            # Skip leading delimiter bytes at buffer start (this is normal -
            # delimiters may arrive in separate BLE notifications)
            leading = 0
            buf_len = len(buf)
            while leading < buf_len and buf[leading] == self.packet_delimiter:
                leading += 1
            if leading:
                del buf[:leading]
                self._stats_delimiters_consumed += leading

            # Re-check buffer length after removing delimiters
            if len(buf) < 4:
                break

            found_valid = False
            search_start = 0

            while search_start < len(buf) - 3:
                # Skip delimiter bytes in the middle of buffer
                if buf[search_start] == self.packet_delimiter:
                    search_start += 1
                    self._stats_delimiters_consumed += 1
                    continue

                pkt_len = buf[search_start]
                if pkt_len == 0:
                    pkt_len = 256

                if pkt_len < 5:
                    search_start += 1
                    continue

                end_pos = search_start + pkt_len
                if end_pos > len(buf):
                    # Incomplete packet - wait for more data
                    break

                # Validate checksum in place; only copy the packet out of
                # the buffer once it passes.
                computed_checksum = checksum3(buf, pkt_len - 3, offset=search_start)
                stored_checksum = three_bytes_to_int(
                    buf[end_pos - 1],
                    buf[end_pos - 2],
                    buf[end_pos - 3]
                )

                if computed_checksum != stored_checksum:
                    # Checksum mismatch - try next byte position
                    search_start += 1
                    continue

                packet = bytes(buf[search_start:end_pos])
                pkt_type = packet[1]

                # Check for packet delimiter after packet
                has_delimiter = end_pos < len(buf) and buf[end_pos] == self.packet_delimiter

                # Handle skipped bytes before valid packet
                if search_start > 0:
                    skipped = buf[:search_start]
                    # Check if all skipped bytes are delimiters (normal)
                    all_delimiters = all(b == self.packet_delimiter for b in skipped)

                    if all_delimiters:
                        self._stats_delimiters_consumed += search_start
                    else:
                        # Actual data loss - non-delimiter bytes discarded
                        self._stats_bytes_discarded += search_start
                        # Only log at debug level to reduce noise - summary shown at end
                        ble_logger.debug(f"Discarded {search_start} bytes before valid packet")

                self._stats_packets_valid += 1

                if self.debug_packets:
                    ble_logger.debug(f"PKT #{self._stats_packets_valid} len={pkt_len} type=0x{pkt_type:02X}")

                parsed = parse_packet(packet)
                if parsed["valid"]:
                    handle_packet(self, parsed)
                    if self.on_packet is not None:
                        try:
                            self.on_packet(parsed["type"], pkt_len)
                        except Exception:
                            pass

                # Consume packet (and delimiter if present)
                if has_delimiter:
                    self._stats_delimiters_consumed += 1
                del buf[:end_pos + (1 if has_delimiter else 0)]
                found_valid = True
                break

            if not found_valid:
                if 0 < search_start < len(buf):
                    # Discard bytes we've already searched past
                    self._stats_bytes_discarded += search_start
                    ble_logger.debug(f"Discarded {search_start} bytes (no valid packet found)")
                    del buf[:search_start]
                break

        # Prevent buffer from growing too large
        # Keep more data to avoid cutting packets (Java keeps bufRXsize - 200)
        if len(buf) > 4096:
            bytes_to_discard = len(buf) - 1024
            self._stats_bytes_discarded += bytes_to_discard
            ble_logger.warning(f"Buffer overflow ({len(buf)} bytes), discarding {bytes_to_discard} bytes")
            del buf[:bytes_to_discard]
    
    def get_rx_stats(self) -> dict:
        """Get receive statistics for diagnostics.
        
        Returns:
            Dictionary with RX statistics
        """
        # Only count actual data bytes discarded (excluding protocol delimiters)
        actual_discarded = self._stats_bytes_discarded
        return {
            'rx_bytes': self._stats_rx_bytes,
            'rx_notifications': self._stats_rx_notifications,
            'packets_valid': self._stats_packets_valid,
            'bytes_discarded': actual_discarded,
            'delimiters': self._stats_delimiters_consumed,
            'buffer_current': len(self.rx_buffer),
            'discard_rate': actual_discarded / max(1, self._stats_rx_bytes) * 100,
        }
    
    def reset_rx_stats(self) -> None:
        """Reset receive statistics."""
        self._stats_rx_bytes = 0
        self._stats_rx_notifications = 0
        self._stats_packets_valid = 0
        self._stats_bytes_discarded = 0
        self._stats_delimiters_consumed = 0
    
    # ==================== COMMAND SENDING ====================
    
    async def send_command(self, command: int, data: bytes = b'', retry: bool = True) -> None:
        """Send command to device with auto-reconnect.
        
        Args:
            command: Command byte
            data: Command data bytes
            retry: If True, attempt reconnect on failure
        """
        if not self.client or not self.client.is_connected:
            if retry and self.auto_reconnect and self.target_address:
                if not await self.ensure_connected():
                    logger.error("Not connected and reconnect failed")
                    return
            else:
                logger.error("Not connected")
                return
        
        packet = create_packet(command, data)
        ble_logger.debug(f"TX: Cmd 0x{command:02X}")
        
        try:
            await self.client.write_gatt_char(TX_CHAR_UUID, packet, response=False)
        except Exception as e:
            logger.error(f"TX error: {e}")
            self.connected = False
            
            if retry and self.auto_reconnect:
                if await self.ensure_connected():
                    await self.send_command(command, data, retry=False)
    
    # ==================== CONNECTION ====================
    
    async def scan_and_connect(self, target_address: Optional[str] = None) -> bool:
        """Scan for and connect to RaySID device.
        
        Args:
            target_address: Optional MAC address to connect to directly
            
        Returns:
            True if connection successful
        """
        target_address = target_address or self.target_address
        
        if not target_address:
            logger.info("Scanning for RaySID devices...")
            
            try:
                devices = await BleakScanner.discover(timeout=3.0, service_uuids=[SERVICE_UUID])
                if devices:
                    target_address = devices[0].address
                    device_name = devices[0].name or 'RaySID'
                    logger.info(f"Found device: {device_name} [{target_address}]")
            except Exception:
                pass
            
            if not target_address:
                logger.info("Scanning by name...")
                devices = await BleakScanner.discover(timeout=3.0)
                for device in devices:
                    if device.name and 'raysid' in device.name.lower():
                        target_address = device.address
                        logger.info(f"Found device: {device.name} [{target_address}]")
                        break
        
        if not target_address:
            logger.error("No RaySID device found. Make sure device is powered on and nearby.")
            return False
        
        logger.info(f"Connecting to {target_address}...")
        self.target_address = target_address
        self.client = BleakClient(target_address, disconnected_callback=self._on_disconnect)
        
        try:
            await self.client.connect()
            logger.info(f"Connected to RaySID [{target_address}]")
            
            await self.client.start_notify(RX_CHAR_UUID, self._notification_handler)
            self.connected = True
            self._intentional_disconnect = False
            
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False
    
    def _on_disconnect(self, client: BleakClient) -> None:
        """Callback when device disconnects unexpectedly."""
        self.connected = False
        if not self._intentional_disconnect and not self._reconnecting:
            logger.warning("Connection lost unexpectedly!")
    
    async def reconnect(self, reinitialize: bool = True) -> bool:
        """Attempt to reconnect to the device.
        
        Args:
            reinitialize: If True, run full initialization after reconnect
            
        Returns:
            True if reconnect successful
        """
        if self._reconnecting:
            return False
        
        self._reconnecting = True
        attempt = 0
        
        logger.info("Attempting to reconnect to device...")
        
        while True:
            attempt += 1
            if (self.max_reconnect_attempts is not None
                    and attempt > self.max_reconnect_attempts):
                logger.error(f"Giving up after {self.max_reconnect_attempts} reconnect attempts")
                self._reconnecting = False
                return False
            
            try:
                if self.client:
                    try:
                        await self.client.disconnect()
                    except:
                        pass
                
                if attempt == 1:
                    logger.info(f"Reconnecting to {self.target_address}...")
                elif attempt % 5 == 0:
                    logger.info(f"Reconnection attempt {attempt}...")
                
                self.client = BleakClient(self.target_address, disconnected_callback=self._on_disconnect)
                await self.client.connect()
                
                await self.client.start_notify(RX_CHAR_UUID, self._notification_handler)
                self.connected = True
                self._intentional_disconnect = False
                
                logger.info(f"Reconnected successfully after {attempt} attempt(s)")
                
                if reinitialize:
                    await self.initialize()
                
                self._reconnecting = False
                return True
                
            except asyncio.CancelledError:
                logger.info("Reconnection cancelled by user")
                self._reconnecting = False
                raise
                
            except Exception as e:
                logger.debug(f"Reconnect attempt {attempt} failed: {e}")
                await asyncio.sleep(self.retry_delay)
    
    async def ensure_connected(self) -> bool:
        """Ensure we're connected, attempt reconnect if not.
        
        Returns:
            True if connected
        """
        if self.connected and self.client and self.client.is_connected:
            return True
        
        if not self.auto_reconnect:
            return False
        
        return await self.reconnect()
    
    async def disconnect(self) -> None:
        """Disconnect from device."""
        self._intentional_disconnect = True
        self.measurement_active = False
        self.connected = False
        
        if self.tick_generator:
            self.tick_generator.stop()
        
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(RX_CHAR_UUID)
                await self.client.disconnect()
                logger.info("Disconnected from device")
            except Exception as e:
                logger.error(f"Error during disconnect: {e}")
    
    # ==================== INITIALIZATION ====================
    
    async def initialize(self, apply_settings: bool = True) -> None:
        """Initialize device communication.

        Performs the full settings sync and enables measurement mode.

        Args:
            apply_settings: push the locally stored settings to the
                device (like the official app does on connect). Pass
                False for read-only use so the device isn't silently
                reconfigured.
        """
        logger.info("Initializing device communication...")
        
        self.rx_buffer.clear()
        
        # Request version
        logger.info("Requesting firmware version...")
        await self.send_command(CMD_VERSION_REQUEST, b'\x00')
        await asyncio.sleep(0.5)
        
        # Log firmware version if received
        if self.device_info.firmware_version:
            logger.info(f"Device firmware: v{self.device_info.firmware_version}")
        
        # Full settings sync (250 steps)
        logger.info("Synchronizing device settings (this may take a few seconds)...")
        for step in range(1, 251):
            await self.send_command(CMD_SETTINGS_SYNC, bytes([step]))
            await asyncio.sleep(0.015)
        if apply_settings:
            logger.info("Applying stored settings to device...")
            await self._apply_stored_settings()
        logger.info("Settings synchronized")

        # Set measurement mode (required for receiving data)
        logger.debug("Setting measurement mode...")
        await self.send_command(CMD_SET_MODE, bytes([3]))
        await asyncio.sleep(0.3)
        
        await self.send_command(CMD_ENABLE_MEASUREMENT, b'')
        await asyncio.sleep(0.3)
        
        logger.info("Initialization complete")
    
    async def _apply_stored_settings(self) -> None:
        """Send all stored settings to the device (registry-driven)."""
        for sdef in ALL_SETTINGS:
            value = getattr(self.settings, sdef.key)
            if not sdef.is_valid(value):
                logger.warning(f"Skipping stored {sdef.key}={value!r} (not a valid device value)")
                continue
            await self.send_command(sdef.command, sdef.encode(value))
            await asyncio.sleep(0.03)

    async def apply_setting(self, key: str, value) -> bool:
        """Set one device setting: validate, send, persist.

        The single code path for changing a device setting — used by the
        TUI menu, the CLI flags, and the set_* convenience methods.

        Args:
            key: DeviceSettings attribute name (see raysid.device_settings)
            value: one of the setting's valid values

        Returns:
            True if the command was sent and the value persisted
        """
        sdef = SETTINGS.get(key)
        if sdef is None:
            logger.error(f"Unknown device setting: {key}")
            return False
        if not sdef.is_valid(value):
            logger.error(
                f"Invalid value for {sdef.label}: {value!r} "
                f"(valid: {', '.join(str(v) for v in sdef.values())})"
            )
            return False
        await self.send_command(sdef.command, sdef.encode(value))
        setattr(self.settings, key, value)
        self.settings.save_to_file()
        if key == "tick_on_client":
            self._sync_tick_generator()
        logger.info(f"{sdef.label} set to {sdef.display(value)}")
        return True

    def _sync_tick_generator(self) -> None:
        """Start/stop client-side Geiger clicks to match tick_on_client.

        Called live when the setting changes (TUI menu / CLI), so the
        switch takes effect immediately. The generator itself starts its
        audio stream lazily on the next measurement update.
        """
        if self.settings.tick_on_client:
            if self.tick_generator is None:
                if not is_audio_available():
                    logger.warning(
                        "Client ticks enabled but audio is not available "
                        "(pip install -e \".[audio]\")")
                    return
                self.tick_generator = TickSoundGenerator(
                    tick_scale=self.settings.clicks_scale)
                logger.info(f"Client ticks on (scale 1:{self.settings.clicks_scale})")
        elif self.tick_generator is not None:
            self.tick_generator.stop()
            self.tick_generator = None
            logger.info("Client ticks off")
    
    # ==================== MEASUREMENT MODE ====================
    
    async def start_measurement(self, tab: int = TAB_SEARCH) -> None:
        """Start continuous measurement.
        
        Args:
            tab: Tab index to use (affects what data device sends)
        """
        self.active_tab = tab
        self.measurement_active = True
        self.rx_buffer.clear()
        
        logger.info("Starting live measurement...")
        
        for _ in range(3):
            await self._send_ping()
            await asyncio.sleep(0.5)
        
        for _ in range(30):
            await asyncio.sleep(0.1)
            if self.device_info.temperature_celsius != 0.0:
                break

        if self._ping_task is not None:
            self._ping_task.cancel()
        self._ping_task = asyncio.create_task(self._ping_loop())
        logger.info("Live measurement active - receiving data from device")

    async def stop_measurement(self) -> None:
        """Stop measurement."""
        self.measurement_active = False
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        logger.info("Measurement stopped")
    
    async def _send_ping(self) -> None:
        """Send ping command."""
        timestamp = int(time.time())
        ping_data = bytes([
            self.active_tab,
            (timestamp >> 24) & 0xFF,
            (timestamp >> 16) & 0xFF,
            (timestamp >> 8) & 0xFF,
            timestamp & 0xFF
        ])
        await self.send_command(CMD_PING, ping_data)
    
    async def _ping_loop(self) -> None:
        """Continuous ping loop with auto-reconnect.

        Note: CMD_ENABLE_MEASUREMENT (0x3E) doubles as the spectrum
        request command, so during a concurrent sync_spectrum() this
        loop harmlessly re-requests spectrum data too.
        """
        while self.measurement_active:
            try:
                if not self.connected or not self.client or not self.client.is_connected:
                    if self.auto_reconnect:
                        logger.info("Connection lost, attempting reconnect...")
                        if await self.ensure_connected():
                            logger.info("Reconnected, resuming measurement...")
                        else:
                            logger.error("Reconnect failed, stopping measurement")
                            break
                    else:
                        break
                
                await self.send_command(CMD_ENABLE_MEASUREMENT, b'')
                await self._send_ping()
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"Ping error: {e}")
                if not self.auto_reconnect:
                    break
                await asyncio.sleep(0.5)
    
    # ==================== SPECTRUM MODE ====================

    def _reset_spectrum_state(self) -> None:
        """Reset acquisition tracking before a new spectrum download."""
        self.spectrum_acquiring = True
        self._spectrum_channel_received = [False] * 1800
        self.spectrum_data = SpectrumData()
        self._spectrum_stats_received = False
        self.rx_buffer.clear()

    def _finalize_spectrum(self) -> None:
        """Stamp metadata on the downloaded spectrum."""
        self.spectrum_data.timestamp = time.time()
        if self.device_info.temperature_celsius != 0.0:
            self.spectrum_data.temperature = self.device_info.temperature_celsius
        if self.settings.spectrum_energy_range in [2, 4, 8]:
            self.spectrum_data.energy_range = self.settings.spectrum_energy_range
        if self.device_info.channel_th_avg > 0:
            self.spectrum_data.channel_th_avg = self.device_info.channel_th_avg

    async def sync_spectrum(self) -> Optional[SpectrumData]:
        """Quick download of current device spectrum.

        Downloads whatever spectrum data the device currently has
        accumulated, then restores the previously active tab so live
        measurement resumes.

        Returns:
            SpectrumData object or None if sync failed
        """
        logger.info("Downloading current spectrum from device...")
        ble_logger.debug(f"sync_spectrum: Starting, connected={self.connected}")

        self._reset_spectrum_state()

        prev_tab = self.active_tab
        self.active_tab = TAB_SPECTRUM
        try:
            await self._send_ping()
            await asyncio.sleep(0.1)

            start_time = time.time()
            timeout = 20.0
            last_request_time = 0
            request_interval = 0.25

            while (time.time() - start_time) < timeout:
                current_time = time.time()

                if not self.connected or not self.client or not self.client.is_connected:
                    if self.auto_reconnect:
                        logger.warning("Connection lost during sync, reconnecting...")
                        if await self.ensure_connected():
                            logger.info("Reconnected, resuming spectrum sync...")
                            self.active_tab = TAB_SPECTRUM
                            await self._send_ping()
                        else:
                            logger.error("Reconnect failed, sync aborted")
                            break
                    else:
                        break

                if current_time - last_request_time >= request_interval:
                    await self.send_command(CMD_SPECTRUM_REQUEST, b'')
                    last_request_time = current_time
                    ble_logger.debug(f"Sent spectrum request, channels so far: {sum(self._spectrum_channel_received)}, buf={len(self.rx_buffer)}")

                await asyncio.sleep(0.05)

                channels_received = sum(self._spectrum_channel_received)

                if channels_received >= 1800 and self._spectrum_stats_received:
                    logger.info("Spectrum download complete")
                    break

                if channels_received >= 1800 and (current_time - start_time) > 8.0:
                    logger.info("Spectrum download complete (stats timeout)")
                    break

                if channels_received >= 1790 and self._spectrum_stats_received and (current_time - start_time) > 10.0:
                    logger.info("Spectrum download complete (~100%)")
                    break
        finally:
            self.spectrum_acquiring = False
            self.active_tab = prev_tab
            try:
                await self._send_ping()
            except Exception:
                pass

        self._finalize_spectrum()

        channels_received = sum(self._spectrum_channel_received)

        if channels_received == 0:
            logger.error("Spectrum sync failed - no data received")
            return None

        if not self._spectrum_stats_received or self.spectrum_data.total_counts == 0:
            self.spectrum_data.total_counts = sum(self.spectrum_data.channels)

        logger.info(f"Spectrum sync complete: {self.spectrum_data.total_counts:,} counts, {channels_received}/1800 channels")
        return self.spectrum_data
    
    async def clear_spectrum(self, energy_range: int = 8) -> None:
        """Clear/restart the device's spectrum accumulator.
        
        Args:
            energy_range: Energy range setting (8=~1000keV, 4=~2100keV, 2=~3500keV)
        """
        logger.info("Clearing device spectrum...")
        await self.send_command(CMD_CLEAR_SPECTRUM, bytes([energy_range]))
        await asyncio.sleep(0.3)
        logger.info(f"Device spectrum cleared ({SETTINGS['spectrum_energy_range'].display(energy_range)})")
    
    # ==================== DOSE OPERATIONS ====================
    
    async def reset_dose(self) -> None:
        """Reset accumulated dose."""
        logger.info("Resetting dose...")
        await self.send_command(CMD_DOSE_RESET, b'')
        await asyncio.sleep(0.5)
        logger.info("Dose reset complete")
    
    # ==================== SETTINGS ====================
    
    async def set_sensitivity(self, value: int) -> bool:
        """Set device sensitivity (1=fast … 64=very accurate)."""
        return await self.apply_setting("update_interval", value)

    async def set_energy_range(self, code: int) -> bool:
        """Set spectrum energy range (8=25-1000keV, 4=30-2000keV, 2=45-3500keV)."""
        return await self.apply_setting("spectrum_energy_range", code)

    async def set_spectrum_channels(self, code: int) -> bool:
        """Set spectrum channel count (1=1800ch, 3=600ch, 9=200ch, 0=auto)."""
        return await self.apply_setting("spectrum_channels", code)

    async def set_ticks_enabled(self, enabled: bool) -> bool:
        """Enable/disable device ticks."""
        return await self.apply_setting("ticks_enabled", bool(enabled))

    async def set_tick_sound(self, enabled: bool) -> bool:
        """Enable/disable device tick sound."""
        return await self.apply_setting("tick_sound", bool(enabled))

    async def set_tick_led(self, enabled: bool) -> bool:
        """Enable/disable device LED flash on tick."""
        return await self.apply_setting("tick_led", bool(enabled))
    
    # ==================== HIGH-LEVEL OPERATIONS ====================
    
    async def get_single_reading(self) -> MeasurementData:
        """Get a single measurement reading."""
        await self.send_command(CMD_ENABLE_MEASUREMENT, b'')
        await self._send_ping()
        await asyncio.sleep(0.5)
        return self.measurement
    
    async def monitor(self, duration: float = 0.0, callback: Optional[Callable] = None) -> None:
        """Monitor radiation for specified duration.
        
        Args:
            duration: Duration in seconds (0 = indefinite)
            callback: Optional callback for each measurement
        """
        if callback is not None:
            self.on_measurement = callback
        elif self.on_measurement is None:
            self.on_measurement = self._default_measurement_callback

        await self.start_measurement()
        
        try:
            if duration > 0:
                await asyncio.sleep(duration)
            else:
                while self.measurement_active:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop_measurement()
    
    def _default_measurement_callback(self, data: MeasurementData) -> None:
        """Default measurement display callback."""
        print(f"\r {data.cps:.2f} CPS | {data.cpm:.1f} CPM | {data.dose_rate_usv:.4f} uSv/h", end="", flush=True)

