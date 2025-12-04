# RaySID BLE Client

A Python command-line client for the RaySID radiation detector. Connects via Bluetooth Low Energy to read live measurements, download gamma spectra, and control device settings.

## ⚠️ Warning

**This is an unofficial, community-made tool.** It was reverse-engineered from the official Android app and is not affiliated with or endorsed by the RaySID manufacturer.

Use at your own risk. This software may:
- Cause unexpected device behavior
- Corrupt device settings
- Potentially brick your device

The author takes no responsibility for any damage to your equipment. If you're not comfortable with these risks, stick to the official app.

---

## Installation

Requires Python 3.8+.

```bash
# Clone or download this directory, then:
cd raysid-client
pip install -e .

# Or install with all optional features:
pip install -e ".[all]"
```

### Optional dependencies

- **Audio support** (Geiger counter clicks): `pip install -e ".[audio]"`
- **Spectrum graphs**: `pip install -e ".[graph]"`
- **Everything**: `pip install -e ".[all]"`

---

## Usage

After installation, the `raysid` command becomes available:

```bash
raysid <command> [options]
```

The device will be auto-discovered via Bluetooth. Make sure it's powered on and nearby.

### Commands

#### `monitor` — Live radiation monitoring

```bash
raysid monitor                    # Continuous monitoring, Ctrl+C to stop
raysid monitor --sound            # With Geiger counter click sounds
raysid monitor -d 60 --csv        # Record 60 seconds to CSV file
raysid monitor --csv -o data.csv  # Save to specific file
```

#### `spectrum` — Gamma spectrum acquisition

```bash
raysid spectrum --sync --graph     # Download current spectrum from device, save PNG
raysid spectrum -t 120 --graph     # Acquire for 2 minutes, then save graph
raysid spectrum --clear --sync     # Clear device memory first, then download
raysid spectrum --delta 60         # Background subtraction mode (60s each phase)
```

#### `info` — Device information

```bash
raysid info                        # Show firmware, battery, temperature, current reading
raysid info --verbose              # With additional debug info
```

#### `settings` — View and change device settings

```bash
raysid settings --show                      # Display all current settings
raysid settings --sensitivity accurate      # Change sensitivity mode
raysid settings --energy-range 4            # Set energy range (2=1MeV, 4=2MeV, 8=3.5MeV)
raysid settings --ticks off                 # Disable device tick sounds
raysid settings --clicks on --clicks-scale 10  # Enable Python-side Geiger clicks
```

#### `dose` — Accumulated dose

```bash
raysid dose                        # Show accumulated dose
raysid dose --reset                # Reset dose counter
```

---

## Common Options

These work with all commands:

| Option | Description |
|--------|-------------|
| `--address`, `-a` | Device MAC address (skip auto-scan) |
| `--verbose`, `-v` | Verbose output |
| `--debug` | Packet-level tracing |
| `--quiet`, `-q` | Suppress non-essential output |
| `--log-file` | Write logs to file |

Sound options (for `monitor`):

| Option | Description |
|--------|-------------|
| `--sound`, `-s` | Enable Geiger counter clicks |
| `--tick-scale` | Click scaling ratio 1:N (default: 20) |
| `--tick-style` | Click sound style: 1-5 (default: 3, DP-5 style) |

---

## Output Formats

### Monitoring data
- **CSV**: Timestamped readings with CPS, CPM, dose rate, temperature, battery
- **JSON**: Same data in JSON format

### Spectrum data
- **PNG**: Graph with optional peak detection
- **CSV**: Channel-by-channel counts with energy calibration
- **JSON**: Full spectrum data including metadata

---

## Programmatic Use

You can also use this as a library:

```python
import asyncio
from raysid import RaySIDClient

async def main():
    client = RaySIDClient()
    
    if await client.scan_and_connect():
        await client.initialize()
        
        # Get a single reading
        reading = await client.get_single_reading()
        print(f"{reading.cps} CPS, {reading.dose_rate_usv} µSv/h")
        
        # Download spectrum
        spectrum = await client.sync_spectrum()
        print(f"{spectrum.total_counts} total counts")
        
        await client.disconnect()

asyncio.run(main())
```

---

## License

MIT License. See `pyproject.toml` for details.

Again: **this is unofficial software.** Use responsibly.

