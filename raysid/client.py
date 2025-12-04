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
    CMD_SET_ENERGY_RANGE,
    CMD_SET_UPDATE_INTERVAL,
    CMD_SET_SPECTRUM_CHANNELS,
    CMD_SET_TICKS_ENABLED,
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
    CMD_ENABLE_MEASUREMENT,
    CMD_DOSE_RESET,
    CMD_SPECTRUM_REQUEST,
    CMD_CALIBRATION_REQUEST,
    CMD_CLEAR_SPECTRUM,
    TAB_SEARCH,
    TAB_SPECTRUM,
    SENSITIVITY_NAMES,
    ENERGY_RANGE_NAMES,
)
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
        retry_delay: float = 2.0
    ):
        """Initialize the RaySID client.
        
        Args:
            verbose: Enable verbose logging
            enable_sound: Enable Geiger counter tick sounds
            debug_packets: Enable detailed packet tracing
            auto_reconnect: Automatically reconnect on connection loss
            retry_delay: Delay between reconnection attempts in seconds
        """
        self.client: Optional[BleakClient] = None
        self.target_address: Optional[str] = None
        self.verbose = verbose
        self.debug_packets = debug_packets
        
        # Auto-reconnect settings
        self.auto_reconnect = auto_reconnect
        self.retry_delay = retry_delay
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
        self.settings_synced = False
        self.active_tab = TAB_SEARCH
        
        # Data
        self.measurement = MeasurementData()
        self.device_info = DeviceInfo()
        self.dose_data = DoseData()
        self.spectrum_data = SpectrumData()
        self.settings = DeviceSettings.load_from_file()
        
        # Callbacks
        self.on_measurement: Optional[Callable[[MeasurementData], None]] = None
        self.on_dose: Optional[Callable[[DoseData], None]] = None
        
        # Spectrum acquisition state
        self.spectrum_position = 0
        self.spectrum_min_channel = 9999
        self.spectrum_last_start_channel = -1
        self.spectrum_acquiring = False
        self._spectrum_stats_received = False
        self._spectrum_full_cycle_count = 0
        self._spectrum_seen_low = False
        self._spectrum_seen_high = False
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
        - Skip leading delimiter bytes (0x3A = ':')
        - Search for valid packet starting at each byte position
        - Validate checksum before consuming packet
        - Track bytes discarded for diagnostic purposes
        """
        max_iterations = 100
        iterations = 0
        
        while len(self.rx_buffer) >= 4 and iterations < max_iterations:
            iterations += 1
            
            # Skip leading delimiter bytes at buffer start (this is normal - 
            # delimiters may arrive in separate BLE notifications)
            while len(self.rx_buffer) > 0 and self.rx_buffer[0] == self.packet_delimiter:
                self.rx_buffer = self.rx_buffer[1:]
                self._stats_delimiters_consumed += 1
            
            # Re-check buffer length after removing delimiters
            if len(self.rx_buffer) < 4:
                break
            
            found_valid = False
            search_start = 0
            
            while search_start < len(self.rx_buffer) - 3:
                # Skip delimiter bytes in the middle of buffer
                if self.rx_buffer[search_start] == self.packet_delimiter:
                    search_start += 1
                    self._stats_delimiters_consumed += 1
                    continue
                
                pkt_len = self.rx_buffer[search_start]
                if pkt_len == 0:
                    pkt_len = 256
                
                if pkt_len < 5:
                    search_start += 1
                    continue
                
                end_pos = search_start + pkt_len
                if end_pos > len(self.rx_buffer):
                    # Incomplete packet - wait for more data
                    break
                
                packet = bytes(self.rx_buffer[search_start:end_pos])
                
                checksum_len = pkt_len - 3
                if checksum_len < 2:
                    search_start += 1
                    continue
                
                computed_checksum = checksum3(packet, checksum_len)
                stored_checksum = three_bytes_to_int(
                    packet[pkt_len - 1],
                    packet[pkt_len - 2],
                    packet[pkt_len - 3]
                )
                
                # Check for packet delimiter after packet
                has_delimiter = False
                if end_pos < len(self.rx_buffer):
                    has_delimiter = self.rx_buffer[end_pos] == self.packet_delimiter
                
                if computed_checksum == stored_checksum and computed_checksum > 0:
                    pkt_type = packet[1] if len(packet) > 1 else 0
                    
                    # Handle skipped bytes before valid packet
                    if search_start > 0:
                        skipped = self.rx_buffer[:search_start]
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
                    
                    # Consume packet (and delimiter if present)
                    consume_len = pkt_len + (1 if has_delimiter else 0)
                    if has_delimiter:
                        self._stats_delimiters_consumed += 1
                    self.rx_buffer = self.rx_buffer[search_start + consume_len:]
                    found_valid = True
                    break
                else:
                    # Checksum mismatch - try next byte position
                    search_start += 1
                    continue
            
            if not found_valid:
                if search_start > 0 and search_start < len(self.rx_buffer):
                    # Discard bytes we've already searched past
                    self._stats_bytes_discarded += search_start
                    ble_logger.debug(f"Discarded {search_start} bytes (no valid packet found)")
                    self.rx_buffer = self.rx_buffer[search_start:]
                break
        
        # Prevent buffer from growing too large
        # Keep more data to avoid cutting packets (Java keeps bufRXsize - 200)
        if len(self.rx_buffer) > 4096:
            bytes_to_discard = len(self.rx_buffer) - 1024
            self._stats_bytes_discarded += bytes_to_discard
            ble_logger.warning(f"Buffer overflow ({len(self.rx_buffer)} bytes), discarding {bytes_to_discard} bytes")
            self.rx_buffer = self.rx_buffer[-1024:]
    
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
    
    async def initialize(self) -> None:
        """Initialize device communication.
        
        Performs full settings sync and enables measurement mode.
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
        self.settings_synced = True
        logger.info("Applying stored settings to device...")
        await self._apply_stored_settings()
        logger.info("Settings synchronized")
        
        # Request calibration
        logger.debug("Requesting calibration data...")
        await self.send_command(CMD_CALIBRATION_REQUEST, b'\x01')
        await asyncio.sleep(0.3)
        
        # Set measurement mode (required for receiving data)
        logger.debug("Setting measurement mode...")
        await self.send_command(CMD_SET_MODE, bytes([3]))
        await asyncio.sleep(0.3)
        
        await self.send_command(CMD_ENABLE_MEASUREMENT, b'')
        await asyncio.sleep(0.3)
        
        logger.info("Initialization complete")
    
    async def _apply_stored_settings(self) -> None:
        """Send stored settings to device."""
        # Spectrum settings
        if self.settings.spectrum_energy_range in [2, 4, 8]:
            await self.send_command(CMD_SET_ENERGY_RANGE, bytes([self.settings.spectrum_energy_range]))
            await asyncio.sleep(0.03)
        
        if self.settings.update_interval in [1, 4, 16, 64]:
            await self.send_command(CMD_SET_UPDATE_INTERVAL, bytes([self.settings.update_interval]))
            await asyncio.sleep(0.03)
        
        if self.settings.spectrum_channels in [0, 1, 3, 9]:
            await self.send_command(CMD_SET_SPECTRUM_CHANNELS, bytes([self.settings.spectrum_channels]))
            await asyncio.sleep(0.03)
        
        # Tick/Click settings
        await self.send_command(CMD_SET_TICKS_ENABLED, bytes([1 if self.settings.ticks_enabled else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_TICK_SOUND, bytes([1 if self.settings.tick_sound else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_TICK_LED, bytes([1 if self.settings.tick_led else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_TICK_ON_CLIENT, bytes([1 if self.settings.tick_on_client else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_TICK_DURATION, bytes([self.settings.tick_duration // 256, self.settings.tick_duration % 256]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_LED_DURATION, bytes([self.settings.led_duration // 256, self.settings.led_duration % 256]))
        await asyncio.sleep(0.03)
        
        # Alarm Level 1 (CPS)
        await self.send_command(CMD_SET_ALARM1_ENABLED, bytes([1 if self.settings.alarm1_enabled else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM1_THRESHOLD, bytes([self.settings.alarm1_threshold // 256, self.settings.alarm1_threshold % 256]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM1_LED, bytes([1 if self.settings.alarm1_led else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM1_SOUND, bytes([1 if self.settings.alarm1_sound else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM1_VIBRO, bytes([1 if self.settings.alarm1_vibro else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM1_ON_CLIENT, bytes([1 if self.settings.alarm1_on_client else 0]))
        await asyncio.sleep(0.03)
        
        # Alarm Level 2 (Dose Rate)
        await self.send_command(CMD_SET_ALARM2_ENABLED, bytes([1 if self.settings.alarm2_enabled else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM2_THRESHOLD, bytes([self.settings.alarm2_threshold // 256, self.settings.alarm2_threshold % 256]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM2_LED, bytes([1 if self.settings.alarm2_led else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM2_SOUND, bytes([1 if self.settings.alarm2_sound else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM2_VIBRO, bytes([1 if self.settings.alarm2_vibro else 0]))
        await asyncio.sleep(0.03)
        await self.send_command(CMD_SET_ALARM2_ON_CLIENT, bytes([1 if self.settings.alarm2_on_client else 0]))
        await asyncio.sleep(0.03)
    
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
        
        asyncio.create_task(self._ping_loop())
        logger.info("Live measurement active - receiving data from device")
    
    async def stop_measurement(self) -> None:
        """Stop measurement."""
        self.measurement_active = False
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
        """Continuous ping loop with auto-reconnect."""
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
    
    async def acquire_spectrum(self, timeout: float = 60.0) -> Optional[SpectrumData]:
        """Acquire spectrum data.
        
        Args:
            timeout: Acquisition timeout in seconds
            
        Returns:
            SpectrumData object or None if acquisition failed
        """
        logger.info(f"Starting spectrum acquisition (timeout: {timeout}s)...")
        
        # Initialize tracking
        self.spectrum_acquiring = True
        self.spectrum_position = 0
        self.spectrum_min_channel = 9999
        self.spectrum_last_start_channel = -1
        self._spectrum_seen_low = False
        self._spectrum_seen_high = False
        self._spectrum_channel_received = [False] * 1800
        self.spectrum_data = SpectrumData()
        self._spectrum_stats_received = False
        self._spectrum_full_cycle_count = 0
        self.rx_buffer.clear()
        
        self.active_tab = TAB_SPECTRUM
        await self._send_ping()
        await asyncio.sleep(0.1)
        
        await self.send_command(CMD_SPECTRUM_REQUEST, b'')
        await asyncio.sleep(0.1)
        
        logger.info("Receiving spectrum data from device...")
        
        start_time = time.time()
        last_request_time = 0
        last_ping_time = 0
        last_log_time = 0
        request_interval = 0.25
        ping_interval = 1.0
        log_interval = 10.0  # Log progress every 10 seconds
        
        while (time.time() - start_time) < timeout:
            current_time = time.time()
            
            if not self.connected or not self.client or not self.client.is_connected:
                if self.auto_reconnect:
                    logger.warning("Connection lost during acquisition, reconnecting...")
                    if await self.ensure_connected():
                        logger.info("Reconnected, resuming spectrum acquisition...")
                        self.active_tab = TAB_SPECTRUM
                        await self._send_ping()
                    else:
                        logger.error("Reconnect failed, acquisition aborted")
                        break
                else:
                    logger.error("Connection lost, acquisition aborted")
                    break
            
            if current_time - last_request_time >= request_interval:
                await self.send_command(CMD_SPECTRUM_REQUEST, b'')
                last_request_time = current_time
            
            if current_time - last_ping_time >= ping_interval:
                await self._send_ping()
                last_ping_time = current_time
            
            # Log progress periodically
            if current_time - last_log_time >= log_interval:
                channels_received = sum(self._spectrum_channel_received)
                total_counts = sum(self.spectrum_data.channels)
                elapsed = current_time - start_time
                remaining = timeout - elapsed
                logger.info(f"Spectrum: {total_counts:,} counts | {channels_received}/1800 ch | {elapsed:.0f}s elapsed, {remaining:.0f}s remaining")
                last_log_time = current_time
            
            await asyncio.sleep(0.05)
        
        self.spectrum_acquiring = False
        self.spectrum_data.timestamp = time.time()
        
        if not self._spectrum_stats_received or self.spectrum_data.total_counts == 0:
            self.spectrum_data.total_counts = sum(self.spectrum_data.channels)
            self.spectrum_data.acquisition_time = timeout
            self.spectrum_data.live_time = timeout
            self.spectrum_data.real_time = timeout
        
        active_channels = sum(1 for c in self.spectrum_data.channels if c > 0)
        
        if active_channels == 0:
            logger.error("Spectrum acquisition failed - no data received")
            return None
        
        if self.device_info.temperature_celsius != 0.0:
            self.spectrum_data.temperature = self.device_info.temperature_celsius
        
        if self.settings.spectrum_energy_range in [2, 4, 8]:
            self.spectrum_data.energy_range = self.settings.spectrum_energy_range
        
        logger.info(f"Spectrum acquisition complete: {self.spectrum_data.total_counts:,} counts in {active_channels} channels")
        return self.spectrum_data
    
    async def sync_spectrum(self) -> Optional[SpectrumData]:
        """Quick download of current device spectrum.
        
        Downloads whatever spectrum data the device currently has accumulated.
        
        Returns:
            SpectrumData object or None if sync failed
        """
        logger.info("Downloading current spectrum from device...")
        ble_logger.debug(f"sync_spectrum: Starting, connected={self.connected}")
        
        self.spectrum_acquiring = True
        self.spectrum_position = 0
        self.spectrum_min_channel = 9999
        self.spectrum_last_start_channel = -1
        self._spectrum_seen_low = False
        self._spectrum_seen_high = False
        self._spectrum_channel_received = [False] * 1800
        self.spectrum_data = SpectrumData()
        self._spectrum_stats_received = False
        self.rx_buffer.clear()
        
        self.active_tab = TAB_SPECTRUM
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
        
        self.spectrum_acquiring = False
        self.spectrum_data.timestamp = time.time()
        
        channels_received = sum(self._spectrum_channel_received)
        
        if channels_received == 0:
            logger.error("Spectrum sync failed - no data received")
            return None
        
        if not self._spectrum_stats_received or self.spectrum_data.total_counts == 0:
            self.spectrum_data.total_counts = sum(self.spectrum_data.channels)
        
        if self.device_info.temperature_celsius != 0.0:
            self.spectrum_data.temperature = self.device_info.temperature_celsius
        
        if self.settings.spectrum_energy_range in [2, 4, 8]:
            self.spectrum_data.energy_range = self.settings.spectrum_energy_range
        
        logger.info(f"Spectrum sync complete: {self.spectrum_data.total_counts:,} counts, {channels_received}/1800 channels")
        return self.spectrum_data
    
    async def clear_spectrum(self, energy_range: int = 8) -> None:
        """Clear/restart the device's spectrum accumulator.
        
        Args:
            energy_range: Energy range setting (8=45-3500keV, 4=30-2000keV, 2=25-1000keV)
        """
        logger.info("Clearing device spectrum...")
        await self.send_command(CMD_CLEAR_SPECTRUM, bytes([energy_range]))
        await asyncio.sleep(0.3)
        logger.info(f"Device spectrum cleared ({ENERGY_RANGE_NAMES.get(energy_range, 'unknown')})")
    
    # ==================== DOSE OPERATIONS ====================
    
    async def reset_dose(self) -> None:
        """Reset accumulated dose."""
        logger.info("Resetting dose...")
        await self.send_command(CMD_DOSE_RESET, b'')
        await asyncio.sleep(0.5)
        logger.info("Dose reset complete")
    
    # ==================== SETTINGS ====================
    
    async def set_sensitivity(self, value: int) -> bool:
        """Set device sensitivity/accuracy.
        
        Args:
            value: 1=Fast, 4=Normal, 16=Accurate, 64=Very Accurate
            
        Returns:
            True if successful
        """
        if value not in [1, 4, 16, 64]:
            logger.error(f"Invalid sensitivity value: {value}")
            return False
        
        logger.info(f"Setting sensitivity to {SENSITIVITY_NAMES[value]}...")
        await self.send_command(CMD_SET_UPDATE_INTERVAL, bytes([value]))
        self.settings.update_interval = value
        self.settings.save_to_file()
        await asyncio.sleep(0.3)
        return True
    
    async def set_energy_range(self, code: int) -> bool:
        """Set spectrum energy range.
        
        Args:
            code: 2=25-1000keV, 4=30-2000keV, 8=45-3500keV
            
        Returns:
            True if successful
        """
        if code not in [2, 4, 8]:
            logger.error(f"Invalid energy range code: {code}")
            return False
        
        logger.info(f"Setting energy range to {ENERGY_RANGE_NAMES[code]}...")
        await self.send_command(CMD_SET_ENERGY_RANGE, bytes([code]))
        self.settings.spectrum_energy_range = code
        self.settings.save_to_file()
        await asyncio.sleep(0.3)
        return True
    
    async def set_spectrum_channels(self, code: int) -> bool:
        """Set spectrum channel count.
        
        Args:
            code: 1=1800ch, 3=600ch, 9=200ch, 0=auto
            
        Returns:
            True if successful
        """
        if code not in [0, 1, 3, 9]:
            logger.error(f"Invalid channel code: {code}")
            return False
        
        await self.send_command(CMD_SET_SPECTRUM_CHANNELS, bytes([code]))
        self.settings.spectrum_channels = code
        self.settings.save_to_file()
        await asyncio.sleep(0.3)
        return True
    
    async def set_ticks_enabled(self, enabled: bool) -> bool:
        """Enable/disable device ticks."""
        await self.send_command(CMD_SET_TICKS_ENABLED, bytes([1 if enabled else 0]))
        self.settings.ticks_enabled = enabled
        self.settings.save_to_file()
        logger.info(f"Device ticks {'enabled' if enabled else 'disabled'}")
        return True
    
    async def set_tick_sound(self, enabled: bool) -> bool:
        """Enable/disable device tick sound."""
        await self.send_command(CMD_SET_TICK_SOUND, bytes([1 if enabled else 0]))
        self.settings.tick_sound = enabled
        self.settings.save_to_file()
        return True
    
    async def set_tick_led(self, enabled: bool) -> bool:
        """Enable/disable device LED flash on tick."""
        await self.send_command(CMD_SET_TICK_LED, bytes([1 if enabled else 0]))
        self.settings.tick_led = enabled
        self.settings.save_to_file()
        return True
    
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
        self.on_measurement = callback or self._default_measurement_callback
        
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

