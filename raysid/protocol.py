"""
RaySID BLE Protocol Constants

Contains all BLE service/characteristic UUIDs, packet types, and commands
based on reverse engineering of BLEservice.java and firmware.

PACKET TYPES:
- 0x02: Status + Temperature + Battery (main status packet)
- 0x06: Dose sync (accumulated dose counters)
- 0x17: Live CPS/Energy data (triplet format)
- 0x1B: Settings sync response
- 0x1F: Firmware version
- 0x45: Spectrum data
- 0x57: Calibration data

Temperature formula: (raw_value / 10.0) - 100.0 = C
"""

# BLE Service and Characteristics
SERVICE_UUID = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
TX_CHAR_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"
RX_CHAR_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"

# Packet Types (from logcat: type=LLTT where LL=length byte, TT=type byte)
PACKET_TYPE_SPECTRUM_STATS = 0x01  # Spectrum statistics (type=1401: 20 bytes)
PACKET_TYPE_STATUS = 0x02          # Status with temp/battery (type=1B02: 27 bytes)
PACKET_TYPE_DOSE_SYNC = 0x06
PACKET_TYPE_SETTINGS_EXT = 0x0A    # Extended settings (type=200A: 32 bytes)
PACKET_TYPE_VERSION_RESP = 0x11    # Version response (type=1411: 20 bytes)
PACKET_TYPE_MEASUREMENT = 0x17     # Live CPS/Energy (type=0C17: 12 bytes)
PACKET_TYPE_SETTINGS_SYNC = 0x1B
PACKET_TYPE_VERSION = 0x1F
PACKET_TYPE_STATUS_EXT = 0x20      # Extended status (type=1220: 18 bytes, very frequent)
PACKET_TYPE_CALIB_RESP = 0x27      # Calibration response (type=0927: 9 bytes)
PACKET_TYPE_LIMITS_RESP = 0x2B     # Spectrum limits (type=102B: 16 bytes)
PACKET_TYPE_SPECTRUM_0 = 0x30      # Spectrum data (type=0030: 256 bytes)
PACKET_TYPE_SPECTRUM_1 = 0x31      # Spectrum data channelsDiv=3
PACKET_TYPE_SPECTRUM_2 = 0x32      # Spectrum data channelsDiv=9
PACKET_TYPE_CALIBRATION = 0x57
PACKET_TYPE_BATTERY = 0x5C

# Commands
CMD_SET_MODE = 0x01
CMD_PING = 0x12
CMD_SETTINGS_SYNC = 0x1B
CMD_VERSION_REQUEST = 0x1F

# Device settings commands (from Main.java onSharedPreferenceChanged)
CMD_SET_ENERGY_RANGE = 0x1E     # Set spectrum energy range: {0x1E, value} where value=2,4,8
CMD_SET_UPDATE_INTERVAL = 0x39  # Set sensitivity: {0x39, value} where value=1,4,16,64
CMD_SET_SPECTRUM_CHANNELS = 0x3F  # Set spectrum channels: {0x3F, value} where value=0,1,3,9

# Tick/Click settings commands (verified against Main.java v1.3.1e
# onSharedPreferenceChanged)
CMD_SET_TICKS_ENABLED = 0x15    # Enable device ticks: {0x15, 0/1}
CMD_SET_TICKS_SCALE = 0x16      # Tick divider 1:N: {0x16, n} (1/5/10/20/50/100/250)
CMD_SET_TICK_SOUND = 0x03       # Device tick sound: {0x03, 0/1}
CMD_SET_TICK_LED = 0x02         # Device LED flashes on tick: {0x02, 0/1}
CMD_SET_TICK_ON_CLIENT = 0x1C   # Client handles tick sounds: {0x1C, 0/1}
CMD_SET_TICK_DURATION = 0x19    # Tick sound TYPE code: {0x19, hi, lo} (1-5, 11-15)
CMD_SET_LED_DURATION = 0x32     # LED tick length: {0x32, hi, lo} (units of 10 ms)

# Alarm Level 1 (CPS) commands
CMD_SET_ALARM1_ENABLED = 0x17   # Enable CPS alarm: {0x17, 0/1}
CMD_SET_ALARM1_THRESHOLD = 0x13 # CPS alarm threshold: {0x13, hi, lo}
CMD_SET_ALARM1_LED = 0x05       # Alarm 1 LED: {0x05, 0/1}
CMD_SET_ALARM1_SOUND = 0x06     # Alarm 1 sound: {0x06, 0/1}
CMD_SET_ALARM1_VIBRO = 0x07     # Alarm 1 vibration: {0x07, 0/1}
CMD_SET_ALARM1_ON_CLIENT = 0x3B # Device ticks during alarm 1: {0x3B, 0/1} (app: level1_device_ticks)

# Alarm Level 2 (Dose Rate) commands
CMD_SET_ALARM2_ENABLED = 0x18   # Enable dose alarm: {0x18, 0/1}
CMD_SET_ALARM2_THRESHOLD = 0x14 # Dose alarm threshold: {0x14, hi, lo} (uSv/h × 100)
CMD_SET_ALARM2_LED = 0x08       # Alarm 2 LED: {0x08, 0/1}
CMD_SET_ALARM2_SOUND = 0x09     # Alarm 2 sound: {0x09, 0/1}
CMD_SET_ALARM2_VIBRO = 0x0A     # Alarm 2 vibration: {0x0A, 0/1}
CMD_SET_ALARM2_ON_CLIENT = 0x3C # Device ticks during alarm 2: {0x3C, 0/1} (app: level2_device_ticks)

CMD_ENABLE_MEASUREMENT = 0x3E
CMD_DOSE_RESET = 0x41
CMD_SET_MEASUREMENT_MODE = 0x43  # Set measurement mode
CMD_SPECTRUM_REQUEST = 0x3E  # Same as enable measurement - triggers spectrum data
CMD_CALIBRATION_REQUEST = 0x57
CMD_CLEAR_SPECTRUM = 0x10  # Clear spectrum: {0x10, energy_range}

# Sensitivity/Accuracy values (update_interval)
SENSITIVITY_FAST = 1        # 1 second averaging - quick response, more noise
SENSITIVITY_NORMAL = 4      # 4 second averaging - balanced (default)
SENSITIVITY_ACCURATE = 16   # 16 second averaging - slower, more stable
SENSITIVITY_VERY_ACCURATE = 64  # 64 second averaging - slowest, most stable

# Tab indices (from Main.java ViewPager)
TAB_SEARCH = 0      # Live measurement
TAB_SPECTRUM = 1    # Spectrum analysis
TAB_MAP = 2         # GPS mapping
TAB_LOG = 3         # Event log
TAB_SETTINGS = 4    # Settings
TAB_DEV = 5         # Developer

# Energy range codes verified against the decompiled app
# (SpectrumUtils.recalculateEnergyCalibration, v1.3.1e): code 8 is the
# NARROW ~1000 keV range ("1k-" calibration files), code 2 the wide
# ~3500 keV one ("3k-"). The keV axis is non-linear — see
# raysid/calibration.py; display labels live in raysid/device_settings.py.

