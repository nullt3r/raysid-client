"""
CLI command implementations for RaySID client.
"""

import json
import os
import time
from datetime import datetime
from typing import List, Optional

from raysid import (
    RaySIDClient,
    MeasurementData,
    SpectrumData,
    DeviceSettings,
)
from raysid.device_settings import SETTINGS, SETTING_GROUPS
from raysid.models import CONFIG_FILE
from raysid.logging_config import get_logger
from raysid.export import (
    MeasurementLogger,
    save_spectrum,
    generate_filename,
)

logger = get_logger('cli')


def print_spectrum_summary(spectrum: SpectrumData) -> None:
    """Print spectrum summary to console."""
    print(f"\n{'='*50}")
    print("Spectrum Acquisition Complete")
    print(f"{'='*50}")
    print(f"Total Counts:     {spectrum.total_counts:,}")
    print(f"Active Channels:  {sum(1 for c in spectrum.channels if c > 0)}")
    if spectrum.channels:
        max_ch = spectrum.channels.index(max(spectrum.channels))
        print(f"Max Counts:       {max(spectrum.channels):,} (ch {max_ch})")
    if spectrum.energy_range in [2, 4, 8]:
        print(f"Energy Range:     {spectrum.get_energy_range_str()}")
    if -50 < spectrum.temperature < 100:
        print(f"Temperature:      {spectrum.temperature:.1f}C")
    else:
        print(f"Temperature:      N/A")


def print_source_identification(spectrum: SpectrumData) -> None:
    """Background verdict, detected peaks + probable sources (same code
    path as the TUI: raysid.background + raysid.nuclides)."""
    from raysid import background as bgmod
    from raysid import nuclides

    bg = bgmod.load_background(spectrum.energy_range)
    if bg is not None and spectrum.real_time > 0:
        net = bgmod.compute_net(spectrum.channels, spectrum.real_time, bg)
        print(f"\nBackground comparison (ref: {bg.counts_sum:,.0f} cts / "
              f"{bg.real_time:.0f}s, {bg.age_hours:.1f}h old):")
        print(f"  {bgmod.verdict_line(net)}")
    elif bg is None:
        print("\n(no background reference — measure one with: raysid background --measure 600)")

    library = nuclides.load_library(spectrum.energy_range)
    if not library:
        print("(no nuclide library imported — run: raysid library --import <apk/xapk>)")
        return

    peaks = nuclides.detect_peaks(
        spectrum.channels, spectrum.real_time, spectrum.total_counts,
        spectrum.energy_range, spectrum.channel_th_avg)
    matches = nuclides.identify(
        spectrum.channels, spectrum.real_time, spectrum.total_counts,
        spectrum.energy_range, spectrum.channel_th_avg, library,
        detected=peaks, bg=bg)

    if peaks:
        print(f"\nDetected Peaks:")
        for p in peaks:
            print(f"  {p.center_kev:>5} keV  (ch {p.center}, {p.cps:.2f} CPS)")

    if matches:
        note = " (background-subtracted)" if bg is not None else ""
        print(f"\nProbable Sources{note} (similarity 0-100):")
        for m in matches[:5]:
            print(f"  {m.similarity:5.1f}  {m.source.name:<22} "
                  f"{m.source.lib:<8} peaks {m.matched_peaks}/{m.total_lib_peaks}")
    elif sum(spectrum.channels) < nuclides.MIN_TICKS_FOR_ID:
        print("\n(identification needs at least 100 counts — accumulate longer)")


async def cmd_background(client: RaySIDClient, opts, args) -> None:
    """Background command - measure or capture a background reference."""
    import asyncio
    from raysid import background as bgmod

    if getattr(args, 'measure', None):
        seconds = args.measure
        print(f"Measuring background for {seconds}s — remove any samples "
              f"from the detector now.")
        await client.clear_spectrum(client.settings.spectrum_energy_range)
        await asyncio.sleep(2)
        remaining = seconds
        while remaining > 0:
            step = min(30, remaining)
            await asyncio.sleep(step)
            remaining -= step
            print(f"  …{remaining}s remaining")

    spectrum = await client.sync_spectrum()
    if not spectrum or not spectrum.real_time:
        print("No spectrum data received — cannot save background")
        return
    spectrum.device_id = client.device_info.device_id

    path = bgmod.save_background(spectrum)
    print(f"\nBackground saved: {path}")
    print(f"  {sum(spectrum.channels):,.0f} counts / {spectrum.real_time:.0f}s "
          f"({sum(spectrum.channels)/spectrum.real_time:.2f} CPS), "
          f"energy range {spectrum.get_energy_range_str()}")


def _measurement_log_paths(args) -> tuple:
    """Resolve (csv_path, json_path) from --csv/--json/-o arguments."""
    save_csv = getattr(args, 'csv', False)
    save_json = getattr(args, 'json', False)
    output_base = getattr(args, 'output', None)
    csv_path = None
    json_path = None
    if save_csv:
        csv_path = output_base if output_base and output_base.endswith('.csv') else \
                  f"{output_base}.csv" if output_base else \
                  generate_filename('raysid_monitor', 'csv')
    if save_json:
        json_path = output_base if output_base and output_base.endswith('.json') else \
                   f"{output_base}.json" if output_base else \
                   generate_filename('raysid_monitor', 'json')
    return csv_path, json_path


async def cmd_tui(client: RaySIDClient, opts, args) -> None:
    """TUI command - the full-screen dashboard (main interface)."""
    from raysid.tui import run_monitor_tui

    csv_path, json_path = _measurement_log_paths(args)
    with MeasurementLogger(csv_path=csv_path, json_path=json_path,
                           device_info=client.device_info) as data_logger:
        def tui_callback(data: MeasurementData):
            data_logger.log(
                data,
                temperature_c=client.device_info.temperature_celsius,
                battery_percent=client.device_info.battery_percent,
            )
        await run_monitor_tui(client, duration=args.duration,
                              on_measurement=tui_callback)


async def cmd_monitor(client: RaySIDClient, opts, args) -> None:
    """Monitor command - live radiation monitoring with optional data logging."""
    print("\n" + "="*60)
    print("RaySID Live Radiation Monitor")
    print("="*60)
    if client.device_info.firmware_version:
        print(f"Firmware: v{client.device_info.firmware_version}")
    if client.tick_generator:
        print(f"Sound: ON (scale 1:{client.tick_generator.tick_scale})")
    
    csv_path, json_path = _measurement_log_paths(args)

    print("Press Ctrl+C to stop\n")
    
    # Use unified MeasurementLogger from export module
    with MeasurementLogger(csv_path=csv_path, json_path=json_path, device_info=client.device_info) as data_logger:
        
        def callback(data: MeasurementData):
            # Get device status
            temp_c = client.device_info.temperature_celsius
            battery = client.device_info.battery_percent
            temp_str = f"{temp_c:.1f}C" if temp_c > -50 else "N/A"
            batt_str = f"{battery}%" if battery > 0 else "N/A"
            
            # Print to console - ALL data as it comes (scientific device!)
            dt = datetime.fromtimestamp(data.timestamp)
            display_time = dt.strftime('%H:%M:%S.%f')[:-3]
            print(f"[{display_time}]  {data.cps:6.2f} CPS | {data.cpm:7.1f} CPM | {data.dose_rate_usv:.4f} uSv/h | Temp: {temp_str} | Bat: {batt_str}", flush=True)
            
            # Log to files if enabled
            data_logger.log(data, temperature_c=temp_c, battery_percent=battery)
        
        # Reset stats before monitoring
        client.reset_rx_stats()
        
        try:
            await client.monitor(duration=args.duration, callback=callback)
        except KeyboardInterrupt:
            print("\n\nMonitoring stopped")
        finally:
            # Show RX statistics only in verbose/debug mode
            if getattr(opts, 'verbose', False) or getattr(opts, 'debug', False):
                stats = client.get_rx_stats()
                print(f"\n📊 RX Stats: {stats['rx_bytes']:,} bytes, {stats['packets_valid']:,} packets, {stats['bytes_discarded']:,} discarded ({stats['discard_rate']:.2f}%)")


async def cmd_spectrum(client: RaySIDClient, opts, args) -> None:
    """Spectrum command - download the device's accumulated spectrum.

    Mirrors the TUI spectrum panel: sync (and optionally clear first),
    print a summary, optionally save the data as CSV/JSON.
    """
    if args.clear:
        await client.clear_spectrum(client.settings.spectrum_energy_range)
        if not args.sync and not (args.output or args.json or args.csv):
            return
        import asyncio
        await asyncio.sleep(0.5)

    spectrum = await client.sync_spectrum()
    if not spectrum:
        print("No spectrum data received")
        return

    spectrum.device_id = client.device_info.device_id

    print_spectrum_summary(spectrum)
    print_source_identification(spectrum)

    if args.output or args.json or args.csv:
        base_name = args.output or f"spectrum_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        saved = save_spectrum(
            spectrum, base_name,
            save_csv=bool(args.csv or args.output),
            save_json=bool(args.json or args.output),
        )
        if saved:
            print(f"\nSaved: {', '.join(saved)}")


async def cmd_info(client: RaySIDClient, opts, args) -> None:
    """Device info command."""
    import asyncio
    
    reading = await client.get_single_reading()
    await asyncio.sleep(0.5)
    
    print("\n" + "="*50)
    print("RaySID Device Information")
    print("="*50)
    print(f"Firmware Version: v{client.device_info.firmware_version}")
    if client.device_info.device_id:
        print(f"Device ID:        {client.device_info.device_id}")
    print(f"Battery:          {client.device_info.battery_percent}%", end="")
    if client.device_info.is_charging:
        print(" (charging)")
    else:
        print()
    print(f"Temperature:      {client.device_info.temperature_celsius:.1f}C")
    
    print(f"\nCurrent Reading:")
    print(f"  CPS:       {reading.cps:.2f}")
    print(f"  CPM:       {reading.cpm:.1f}")
    print(f"  Dose Rate: {reading.dose_rate_usv:.4f} uSv/h")
    print(f"  Temp OK:   {reading.temperature_ok}")
    print(f"  Spectrum:  {'Full' if reading.spectrum_full else 'Recording'}")
    
    print(f"\nCurrent Settings:")
    for key, label in (("alarm1_threshold", "CPS Alarm"),
                       ("alarm2_threshold", "Dose Alarm"),
                       ("ticks_enabled", "Device Ticks"),
                       ("spectrum_energy_range", "Spectrum Range")):
        sdef = SETTINGS[key]
        print(f"  {label + ':':<18}{sdef.display(getattr(client.settings, key))}")


async def cmd_settings(client: RaySIDClient, opts, args) -> None:
    """Settings command - view and modify settings."""
    
    if args.show:
        print("\n" + "="*60)
        print("RaySID Settings")
        print("="*60)

        # Device settings — one listing driven by the same registry the
        # TUI menu and client.apply_setting() use.
        for group, defs in SETTING_GROUPS:
            print(f"\n{group} (sent to device):")
            for sdef in defs:
                value = getattr(client.settings, sdef.key)
                print(f"  {sdef.key:<24}{sdef.label + ':':<22}{sdef.display(value)}")

        print("\nClient Settings (local only):")
        print(f"  Clicks Scale:       1:{client.settings.clicks_scale}")
        
        print(f"\nConfig file: {CONFIG_FILE}")
        return
    
    # Set individual settings — every device setting goes through
    # client.apply_setting(), the same code path the TUI menu uses.
    changed = False

    async def apply(key: str, value) -> None:
        nonlocal changed
        sdef = SETTINGS[key]
        if await client.apply_setting(key, value):
            print(f"{sdef.label}: {sdef.display(value)}")
            changed = True
        else:
            valid = ", ".join(str(v) for v in sdef.values())
            print(f"{sdef.label}: invalid value {value!r} (valid: {valid})")

    if args.sensitivity is not None:
        sensitivity_map = {"fast": 1, "normal": 4, "accurate": 16, "very-accurate": 64}
        await apply("update_interval", sensitivity_map[args.sensitivity])

    if args.energy_range is not None:
        await apply("spectrum_energy_range", args.energy_range)

    if args.channels is not None:
        channels_to_code = {1800: 1, 600: 3, 200: 9, 0: 0}
        await apply("spectrum_channels", channels_to_code.get(args.channels, args.channels))

    if args.ticks is not None:
        await apply("ticks_enabled", args.ticks)

    if args.tick_sound is not None:
        await apply("tick_sound", args.tick_sound)

    if args.tick_led is not None:
        await apply("tick_led", args.tick_led)

    # Generic access to ANY device setting: --set key=value
    for key, raw in getattr(args, 'set_pairs', ()):
        sdef = SETTINGS.get(key)
        if sdef is None:
            print(f"Unknown setting: {key}")
            print(f"Valid keys: {', '.join(sorted(SETTINGS))}")
            continue
        if sdef.encoding == "bool":
            value = raw.lower() in ("on", "true", "1", "yes")
        else:
            try:
                value = int(raw)
            except ValueError:
                print(f"{sdef.label}: {raw!r} is not a number")
                continue
        await apply(key, value)

    if args.clicks is not None:
        await apply("tick_on_client", args.clicks)
    
    if args.clicks_scale is not None:
        client.settings.clicks_scale = args.clicks_scale
        if client.tick_generator:
            client.tick_generator.tick_scale = args.clicks_scale
        client.settings.save_to_file()
        print(f"Clicks scale set to: 1:{args.clicks_scale}")
        changed = True

    if not changed and not args.show:
        print("No settings changed. Use --show to view current settings.")


async def cmd_dose(client: RaySIDClient, opts, args) -> None:
    """Dose command - show accumulated dose or reset counter."""
    import asyncio
    
    if args.reset:
        print("\nResetting dose counter...")
        await client.reset_dose()
        print("Dose counter reset.")
        return
    
    # Start brief monitoring to receive dose data
    print("\nReading dose data from device...")
    print("(Device sends dose updates during measurement)")
    
    dose_received = [False]
    measurement_count = [0]
    
    def dose_callback(data):
        dose_received[0] = True
        logger.info(f"Dose packet received: {data.dose_usv:.4f} uSv")
    
    def measurement_callback(data):
        measurement_count[0] += 1
    
    client.on_dose = dose_callback
    client.on_measurement = measurement_callback
    
    # Start measurement to trigger dose sync
    await client.start_measurement()
    
    # Wait for dose data (up to 10 seconds, or until we get it)
    for i in range(100):
        if dose_received[0]:
            break
        if i % 10 == 0 and i > 0:
            print(f"  Waiting for dose packet... ({measurement_count[0]} measurements received)")
        await asyncio.sleep(0.1)
    
    await client.stop_measurement()
    
    if not dose_received[0]:
        print("\nNote: No dose sync packet received from device.")
        print("The device may need to be in measurement mode longer,")
        print("or try: raysid monitor (and check dose in the output)")
    
    # Display dose information
    print(f"\n{'='*50}")
    print("Accumulated Dose (from device)")
    print(f"{'='*50}")
    print(f"Total Dose:        {client.dose_data.dose_usv:.6f} uSv")
    print(f"                   {client.dose_data.dose_usv * 1000:.3f} nSv")
    print(f"Dose Count:        {client.dose_data.dose_count}")
    print(f"Dose Sum (raw):    {client.dose_data.dose_sum}")
    
    if client.dose_data.dose_count > 0 and client.dose_data.dose_usv > 0:
        avg_per_reading = client.dose_data.dose_usv / client.dose_data.dose_count * 1000000
        print(f"Average/Reading:   {avg_per_reading:.2f} nSv")
    
    print(f"{'='*50}")

