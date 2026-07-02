# RaySID BLE Client

An unofficial command-line and terminal-UI client for the **RaySID** gamma
radiation detector. Connect over Bluetooth Low Energy to read live
measurements, download and analyze gamma spectra, identify radioactive
sources, and control every device setting — from your computer instead of
the phone app.

The BLE protocol was reverse-engineered from the official Android app by
[@nullt3r](https://github.com/nullt3r) with the help of Claude (Anthropic).
The result offers **most of the functionality of the mobile app** —
live monitoring, spectrum acquisition, energy calibration, nuclide
identification, background comparison, and full settings control — **except
firmware updates**, which this client intentionally does not attempt.

## ⚠️ Warning

**This is unofficial, community-made software.** It is not affiliated with
or endorsed by the RaySID manufacturer. It talks directly to your device
over an undocumented protocol, so it may cause unexpected behavior or
corrupt device settings. Use at your own risk; if you're not comfortable
with that, use the official app. The author takes no responsibility for any
damage to your equipment.

---

## What it can do

- **Live monitoring** — CPS, CPM, dose rate (µSv/h), temperature, battery,
  with optional Geiger-counter click sounds and CSV/JSON logging.
- **Gamma spectra** — download the device's accumulated 1800-channel
  spectrum with correct (non-linear) energy calibration ported from the app.
- **Source identification** — detect spectral peaks and match them against
  the app's own reference library (Cs-137, Co-60, K-40, uranium/thorium
  ores, uranium glass, medical isotopes, …) using a faithful port of the
  app's detection and matching algorithm.
- **Background discrimination** — measure a background reference and get a
  statistically sound "source detected / not detected" verdict (Currie /
  ISO 11929), plus a net-spectrum view that shows what's really above
  background and what's just noise.
- **Full device control** — sensitivity, energy range, spectrum channels,
  tick sounds and LED, and both alarm levels — the same options as the app.
- **Two front-ends, one engine** — a full-screen terminal dashboard and a
  scriptable CLI, both driving the same code.

---

## Installation

Requires Python 3.8+.

```bash
git clone <this-repo>
cd raysid-client
pip install -e ".[all]"      # everything (recommended)
```

Or pick what you need:

```bash
pip install -e .             # core client + CLI only
pip install -e ".[tui]"      # + the full-screen dashboard
pip install -e ".[audio]"    # + Geiger click sounds
```

The device is auto-discovered over Bluetooth — just make sure it's powered
on and nearby. On Linux you may need to be in the `bluetooth` group; on
macOS grant Bluetooth permission to your terminal.

---

## Quick start

```bash
raysid                # launch the full-screen dashboard (the main UI)
raysid monitor        # or a plain scrolling console monitor
raysid info           # one-shot: firmware, battery, temperature, reading
```

---

## The dashboard (`raysid` / `raysid tui`)

Running `raysid` with no arguments opens the terminal dashboard: live
measurement, a calibrated spectrum plot, packet log, and device
diagnostics, all updating in real time. It needs the `tui` extra.

Controls are function keys (with a clickable footer, Total-Commander style;
plain digits and letters work as aliases):

| Key | Action |
|-----|--------|
| `F2` | Device settings menu — sensitivity, energy range, ticks, alarms |
| `F5` | Sync the spectrum from the device now |
| `F6` | Save the current spectrum as the **background** reference |
| `F7` | Toggle the **net** view (spectrum minus background, colored by significance) |
| `F8` | Clear the device's accumulated spectrum |
| `F9` | Reset RX statistics |
| `C`  | Clear the packet log |
| `F10` / `q` | Quit |

The settings menu (`F2`) is fully keyboard- and mouse-driven and changes
are sent to the device immediately. The SOURCE ID panel shows the detection
verdict and the most probable sources once a spectrum has been synced.

---

## Telling a real source from background

This is the hard part of any hobby detector, and the client is built around
doing it correctly rather than by eye. Radioactive counts are Poisson, so a
"looks a bit higher" reading means nothing without statistics.

**1. Import the reference library once** (needed for source names). The
reference spectra are proprietary app data and are *not* bundled — extract
them from your own copy of the official app package:

```bash
raysid library --import Raysid_App.xapk    # or .apk; stored in ~/.raysid_library/
raysid library                             # show what's imported
```

**2. Measure a background** somewhere with no sample present. Longer is
better — the detection limit falls with √time; 10 minutes is a good start:

```bash
raysid background --measure 600            # clear, accumulate 10 min, save
raysid background                          # show stored backgrounds
```

**3. Measure your sample and read the verdict:**

```bash
raysid spectrum --clear --sync             # fresh spectrum of the sample
```

You'll get a Currie detection verdict at 95 % confidence
(`SOURCE DETECTED` / `no source above background`), the detected peaks in
keV, and the probable sources — matched on the **background-subtracted**
spectrum, which is what makes weak samples identifiable.

In the dashboard the same workflow is `F6` (save background) → `F8` (clear)
→ let the sample accumulate → `F7` to see the net spectrum, with the SOURCE
ID panel updating live. Good test samples: uranium glass ("vaseline
glass"), old radium watch dials, thoriated welding rods, potassium-rich
salt substitute (K-40).

---

## All commands

| Command | What it does |
|---------|--------------|
| `raysid` / `raysid tui` | Full-screen dashboard (main interface) |
| `raysid monitor` | Plain console live monitoring |
| `raysid spectrum` | Download a spectrum, show summary + source ID |
| `raysid background` | Measure / manage the background reference |
| `raysid library` | Import / show the nuclide reference library |
| `raysid settings` | View and change device settings |
| `raysid info` | Firmware, battery, temperature, current reading |
| `raysid dose` | Show or reset accumulated dose |

Run `raysid <command> --help` for the full option list. Some highlights:

```bash
raysid monitor -d 60 --csv                 # log 60 s to a timestamped CSV
raysid monitor --sound                     # with Geiger click sounds
raysid spectrum --csv --json               # save spectrum data files
raysid settings --show                     # list every current setting
raysid settings --sensitivity accurate     # change a setting
raysid settings --energy-range 8           # 8 = narrow (~1000 keV), 2 = wide (~3500 keV)
raysid settings --set alarm1_threshold=100 # set any setting by key
raysid dose --reset                        # reset the dose counter
```

**Note on connect behavior:** commands that measure
(tui / monitor / spectrum / background / settings) push your locally stored
settings to the device on connect, exactly like the official app does — so
if you changed something in the app in the meantime, it gets overwritten.
The read-only commands (`info`, `dose`) never reconfigure the device.

---

## Output formats

- **Monitoring** → CSV / JSON: timestamped CPS, CPM, dose rate,
  temperature, battery.
- **Spectra** → CSV / JSON: per-channel counts with energy calibration and
  full acquisition metadata. The CSV loads directly into NumPy, pandas, or a
  spreadsheet; for deep analysis, tools like InterSpec read the exported
  data.

---

## Programmatic use

The client is a normal async Python library:

```python
import asyncio
from raysid import RaySIDClient

async def main():
    client = RaySIDClient()
    if await client.scan_and_connect():
        await client.initialize()

        reading = await client.get_single_reading()
        print(f"{reading.cps:.2f} CPS, {reading.dose_rate_usv:.4f} µSv/h")

        spectrum = await client.sync_spectrum()
        print(f"{spectrum.total_counts:,} total counts")

        await client.disconnect()

asyncio.run(main())
```

For source identification and background statistics, see
`raysid.nuclides` and `raysid.background`.

---

## How it works

The `raysid` package is layered: `protocol` (constants) → `handlers`
(packet parsing) → `client` (BLE session and state) → analysis modules
(`calibration`, `nuclides`, `background`, `device_settings`) → front-ends
(`cli`, `tui`). The energy calibration, peak detection, and nuclide
matching are direct ports of the algorithms in the official app, validated
against the app's own reference data.

---

## License

MIT License — see `pyproject.toml`. Unofficial software; use responsibly.
