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
from raysid.protocol import ENERGY_RANGE_NAMES, SENSITIVITY_NAMES, CHANNEL_NAMES
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
    
    peaks = spectrum.find_peaks(threshold=0.05, min_distance=30)
    if peaks:
        print(f"\nDetected Peaks (top 5):")
        for i, peak in enumerate(peaks[:5]):
            print(f"  {i+1}. {peak['energy_kev']:.1f} keV (ch {peak['channel']}, {peak['counts']:,} counts)")


async def cmd_monitor(client: RaySIDClient, opts, args) -> None:
    """Monitor command - live radiation monitoring with optional data logging."""
    print("\n" + "="*60)
    print("RaySID Live Radiation Monitor")
    print("="*60)
    if client.device_info.firmware_version:
        print(f"Firmware: v{client.device_info.firmware_version}")
    if client.tick_generator:
        print(f"Sound: ON (scale 1:{client.tick_generator.tick_scale})")
    
    # Setup file logging using unified export module
    save_csv = getattr(args, 'csv', False)
    save_json = getattr(args, 'json', False)
    output_base = getattr(args, 'output', None)
    
    # Generate filenames
    csv_path = None
    json_path = None
    
    if save_csv or save_json:
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        if save_csv:
            csv_path = output_base if output_base and output_base.endswith('.csv') else \
                      f"{output_base}.csv" if output_base else \
                      generate_filename('raysid_monitor', 'csv')
        
        if save_json:
            json_path = output_base if output_base and output_base.endswith('.json') else \
                       f"{output_base}.json" if output_base else \
                       generate_filename('raysid_monitor', 'json')
    
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
    """Spectrum acquisition command with graph and CSV export."""
    
    wants_output = args.output or args.json or args.csv or args.graph
    
    # Handle --clear option
    if args.clear:
        await client.clear_spectrum(client.settings.spectrum_energy_range)
        if not args.sync and not wants_output and not args.interval:
            print("Spectrum cleared. Use --graph, --csv, or --timeout to acquire after clearing.")
            return
        import asyncio
        await asyncio.sleep(0.5)
    
    # Handle --sync option
    if args.sync:
        spectrum = await client.sync_spectrum()
        if not spectrum:
            print("No spectrum data to sync")
            return
        
        spectrum.energy_range = client.settings.spectrum_energy_range
        spectrum.device_id = client.device_info.device_id
        spectrum.temperature = client.device_info.temperature_celsius
        
        print_spectrum_summary(spectrum)
        
        if args.output or args.json or args.csv or args.graph:
            base_name = args.output or f"spectrum_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            if base_name.endswith('.json') or base_name.endswith('.csv') or base_name.endswith('.png'):
                base_name = base_name.rsplit('.', 1)[0]
            
            save_json = args.json or args.output
            save_csv = args.csv or args.output
            save_graph = args.graph or args.output
            
            title = args.title or f"RaySID Gamma Spectrum ({spectrum.get_energy_range_str()})"
            saved = save_spectrum(spectrum, base_name, save_csv=save_csv, save_json=save_json, save_graph=save_graph,
                                  log_scale=args.log_scale, show_peaks=not args.no_peaks, title=title)
            if saved:
                print(f"\nSaved: {', '.join(saved)}")
        return
    
    # Continuous mode
    if args.interval and args.interval > 0:
        await cmd_spectrum_continuous(client, opts, args)
        return
    
    # Delta mode
    if hasattr(args, 'delta') and args.delta and args.delta > 0:
        await cmd_spectrum_delta(client, opts, args)
        return
    
    # Single acquisition
    print(f"Acquiring spectrum (timeout: {args.timeout}s)...")
    spectrum = await client.acquire_spectrum(timeout=args.timeout)
    
    if not spectrum:
        print("Acquisition failed or timed out")
        return
    
    spectrum.energy_range = client.settings.spectrum_energy_range
    spectrum.device_id = client.device_info.device_id
    spectrum.temperature = client.device_info.temperature_celsius
    
    print_spectrum_summary(spectrum)
    
    base_name = args.output or f"spectrum_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if base_name.endswith('.json') or base_name.endswith('.csv') or base_name.endswith('.png'):
        base_name = base_name.rsplit('.', 1)[0]
    
    save_json = args.json or args.output
    save_csv = args.csv or args.output
    save_graph = args.graph or args.output
    
    title = args.title or f"RaySID Gamma Spectrum ({spectrum.get_energy_range_str()})"
    saved = save_spectrum(spectrum, base_name, save_csv=save_csv, save_json=save_json, save_graph=save_graph,
                          log_scale=args.log_scale, show_peaks=not args.no_peaks, title=title)
    
    if saved:
        print(f"\nSaved: {', '.join(saved)}")


async def cmd_spectrum_delta(client: RaySIDClient, opts, args) -> None:
    """Delta spectrum mode: subtract background from sample measurement."""
    import asyncio
    
    acq_time = args.delta
    
    print(f"\n{'='*60}")
    print("Delta Spectrum Mode (Background Subtraction)")
    print(f"{'='*60}")
    print(f"Acquisition time: {acq_time} seconds per phase")
    print(f"{'='*60}\n")
    
    # Phase 1: Background
    print("=" * 50)
    print("PHASE 1: BACKGROUND MEASUREMENT")
    print("=" * 50)
    print("Remove any radioactive sources from the area.")
    print("Press ENTER when ready...")
    input()
    
    print("\nClearing device spectrum...")
    await client.clear_spectrum(client.settings.spectrum_energy_range)
    await asyncio.sleep(1.0)
    
    print(f"Measuring background for {acq_time} seconds...")
    background = await client.acquire_spectrum(timeout=acq_time)
    
    if not background:
        print("Background acquisition failed")
        return
    
    background.energy_range = client.settings.spectrum_energy_range
    bg_counts = sum(background.channels)
    bg_cps = bg_counts / acq_time if acq_time > 0 else 0
    print(f"Background: {bg_counts:,} counts ({bg_cps:.1f} CPS)")
    
    # Phase 2: Sample
    print("\n" + "=" * 50)
    print("PHASE 2: SAMPLE MEASUREMENT")
    print("=" * 50)
    print("Place the radioactive sample near the detector.")
    print("Press ENTER when ready...")
    input()
    
    print("\nClearing device spectrum...")
    await client.clear_spectrum(client.settings.spectrum_energy_range)
    await asyncio.sleep(1.0)
    
    print(f"Measuring sample for {acq_time} seconds...")
    sample = await client.acquire_spectrum(timeout=acq_time)
    
    if not sample:
        print("Sample acquisition failed")
        return
    
    sample.energy_range = client.settings.spectrum_energy_range
    sample_counts = sum(sample.channels)
    sample_cps = sample_counts / acq_time if acq_time > 0 else 0
    print(f"Sample: {sample_counts:,} counts ({sample_cps:.1f} CPS)")
    
    # Phase 3: Calculate difference
    print("\n" + "=" * 50)
    print("PHASE 3: CALCULATING DIFFERENCE")
    print("=" * 50)
    
    delta_spectrum = sample.subtract(background, normalize=True)
    
    positive_sum = sum(c for c in delta_spectrum.channels if c > 0)
    negative_sum = sum(c for c in delta_spectrum.channels if c < 0)
    net_delta = sum(delta_spectrum.channels)
    
    print(f"\nResults:")
    print(f"  Above background: {positive_sum:,} counts")
    print(f"  Below background: {negative_sum:,} counts")
    print(f"  Net difference:   {net_delta:+,} counts")
    
    print_spectrum_summary(delta_spectrum)
    
    # Save outputs
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_name = args.output or f"spectrum_delta_{timestamp}"
    if base_name.endswith('.json') or base_name.endswith('.csv') or base_name.endswith('.png'):
        base_name = base_name.rsplit('.', 1)[0]
    
    title = args.title or f"RaySID Delta Spectrum (Background Subtracted)"
    saved = save_spectrum(delta_spectrum, base_name, save_csv=args.csv, save_json=True, save_graph=True,
                          log_scale=args.log_scale, show_peaks=not args.no_peaks, title=title)
    
    # Save background and sample
    save_spectrum(background, f"{base_name}_background", save_csv=False, save_json=True, save_graph=False)
    save_spectrum(sample, f"{base_name}_sample", save_csv=False, save_json=True, save_graph=False)
    
    if saved:
        print(f"\nDelta spectrum saved: {', '.join(saved)}")
        print(f"Background saved: {base_name}_background.json")
        print(f"Sample saved: {base_name}_sample.json")


async def cmd_spectrum_continuous(client: RaySIDClient, opts, args) -> None:
    """Continuous spectrum acquisition with periodic saving."""
    import asyncio
    
    interval = args.interval
    duration = args.duration if hasattr(args, 'duration') and args.duration else 0
    
    print(f"\n{'='*60}")
    print("Continuous Spectrum Acquisition")
    print(f"{'='*60}")
    print(f"Save interval:  {interval} seconds")
    print(f"Duration:       {'indefinite' if duration == 0 else f'{duration} seconds'}")
    print(f"\nPress Ctrl+C to stop")
    print(f"{'='*60}\n")
    
    output_dir = args.output or "spectrum_data"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")
    
    start_time = time.time()
    save_count = 0
    last_save_time = start_time
    
    save_json = args.json or True
    save_csv = args.csv
    save_graph = args.graph
    
    try:
        while True:
            current_time = time.time()
            elapsed = current_time - start_time
            
            if duration > 0 and elapsed >= duration:
                print(f"\nDuration limit reached ({duration}s)")
                break
            
            if current_time - last_save_time >= interval:
                save_count += 1
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                
                print(f"\nAcquiring spectrum #{save_count}...")
                spectrum = await client.acquire_spectrum(timeout=args.timeout)
                
                if spectrum:
                    spectrum.energy_range = client.settings.spectrum_energy_range
                    spectrum.device_id = client.device_info.device_id
                    spectrum.temperature = client.device_info.temperature_celsius
                    spectrum.acquisition_time = elapsed
                    
                    base_name = f"{output_dir}/spectrum_{timestamp}_{save_count:04d}"
                    title = f"Spectrum #{save_count} ({spectrum.get_energy_range_str()})"
                    
                    save_spectrum(spectrum, base_name, save_csv=save_csv, save_json=save_json, save_graph=save_graph,
                                  log_scale=args.log_scale, show_peaks=not args.no_peaks, title=title)
                    
                    print(f"#{save_count}: {spectrum.total_counts:,} counts, saved to {output_dir}/")
                else:
                    print(f"#{save_count}: Acquisition failed")
                
                last_save_time = current_time
            
            time_to_next = interval - (current_time - last_save_time)
            print(f"\rNext save in {time_to_next:.0f}s (elapsed: {elapsed:.0f}s)   ", end="", flush=True)
            
            await asyncio.sleep(1)
            
    except asyncio.CancelledError:
        pass
    
    print(f"\n\nContinuous acquisition stopped")
    print(f"Total acquisitions: {save_count}")
    print(f"Total time: {time.time() - start_time:.0f} seconds")
    print(f"Data saved to: {output_dir}/")


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
    print(f"  CPS Alarm:        {client.settings.get_alarm1_threshold_str()}")
    print(f"  Dose Alarm:       {client.settings.get_alarm2_threshold_str()}")
    print(f"  Device Ticks:     {'Enabled' if client.settings.ticks_enabled else 'Disabled'}")
    print(f"  Spectrum Range:   {ENERGY_RANGE_NAMES.get(client.settings.spectrum_energy_range, 'Unknown')}")
    print(f"  Units:            {client.settings.dose_rate_units}")


async def cmd_settings(client: RaySIDClient, opts, args) -> None:
    """Settings command - view and modify settings."""
    
    if args.show:
        print("\n" + "="*60)
        print("RaySID Settings")
        print("="*60)
        
        print("\nSpectrum Settings (sent to device):")
        print(f"  Sensitivity:        {client.settings.get_sensitivity_name()} ({client.settings.update_interval}s averaging)")
        print(f"  Energy Range:       {ENERGY_RANGE_NAMES.get(client.settings.spectrum_energy_range, 'Unknown')}")
        print(f"  Spectrum Channels:  {CHANNEL_NAMES.get(client.settings.spectrum_channels, 'Unknown')}")
        
        print("\nTick/Click Settings (sent to device):")
        print(f"  Device Ticks:       {'Enabled' if client.settings.ticks_enabled else 'Disabled'}")
        print(f"  Tick Sound:         {'On' if client.settings.tick_sound else 'Off'}")
        print(f"  Tick LED Flash:     {'On' if client.settings.tick_led else 'Off'}")
        print(f"  Tick on Client:     {'On' if client.settings.tick_on_client else 'Off'}")
        print(f"  Tick Duration:      {client.settings.tick_duration} ms")
        print(f"  LED Duration:       {client.settings.led_duration * 10} ms")
        
        print("\nAlarm 1 - CPS (sent to device):")
        print(f"  Enabled:            {'Yes' if client.settings.alarm1_enabled else 'No'}")
        print(f"  Threshold:          {client.settings.get_alarm1_threshold_str()}")
        print(f"  LED:                {'On' if client.settings.alarm1_led else 'Off'}")
        print(f"  Sound:              {'On' if client.settings.alarm1_sound else 'Off'}")
        print(f"  Vibration:          {'On' if client.settings.alarm1_vibro else 'Off'}")
        print(f"  On Client:          {'On' if client.settings.alarm1_on_client else 'Off'}")
        
        print("\nAlarm 2 - Dose Rate (sent to device):")
        print(f"  Enabled:            {'Yes' if client.settings.alarm2_enabled else 'No'}")
        print(f"  Threshold:          {client.settings.get_alarm2_threshold_str()}")
        print(f"  LED:                {'On' if client.settings.alarm2_led else 'Off'}")
        print(f"  Sound:              {'On' if client.settings.alarm2_sound else 'Off'}")
        print(f"  Vibration:          {'On' if client.settings.alarm2_vibro else 'Off'}")
        print(f"  On Client:          {'On' if client.settings.alarm2_on_client else 'Off'}")
        
        print("\nClient Settings (local only):")
        print(f"  Python Clicks:      {'Enabled' if client.settings.clicks_enabled else 'Disabled'}")
        print(f"  Clicks Scale:       1:{client.settings.clicks_scale}")
        print(f"  Spectrum Scale:     {client.settings.spectrum_scale}")
        print(f"  Dose Rate Units:    {client.settings.dose_rate_units}")
        
        print(f"\nConfig file: {CONFIG_FILE}")
        return
    
    # Set individual settings
    changed = False
    
    if args.sensitivity is not None:
        sensitivity_map = {"fast": 1, "normal": 4, "accurate": 16, "very-accurate": 64}
        value = sensitivity_map[args.sensitivity]
        await client.set_sensitivity(value)
        changed = True
    
    if args.energy_range is not None:
        kev_to_code = {1000: 2, 2000: 4, 3500: 8}
        code = kev_to_code[args.energy_range]
        await client.set_energy_range(code)
        changed = True
    
    if args.channels is not None:
        channels_to_code = {1800: 1, 600: 3, 200: 9, 0: 0}
        code = channels_to_code[args.channels]
        await client.set_spectrum_channels(code)
        changed = True
    
    if args.ticks is not None:
        await client.set_ticks_enabled(args.ticks)
        changed = True
    
    if args.tick_sound is not None:
        await client.set_tick_sound(args.tick_sound)
        changed = True
    
    if args.tick_led is not None:
        await client.set_tick_led(args.tick_led)
        changed = True
    
    if hasattr(args, 'tick_duration') and args.tick_duration is not None:
        if 1 <= args.tick_duration <= 1000:
            from raysid.protocol import CMD_SET_TICK_DURATION
            await client.send_command(CMD_SET_TICK_DURATION, bytes([args.tick_duration // 256, args.tick_duration % 256]))
            client.settings.tick_duration = args.tick_duration
            client.settings.save_to_file()
            print(f"Tick duration set to: {args.tick_duration} ms")
            changed = True
        else:
            print("Tick duration must be 1-1000 ms")
    
    if hasattr(args, 'led_duration') and args.led_duration is not None:
        if 1 <= args.led_duration <= 100:
            from raysid.protocol import CMD_SET_LED_DURATION
            await client.send_command(CMD_SET_LED_DURATION, bytes([args.led_duration // 256, args.led_duration % 256]))
            client.settings.led_duration = args.led_duration
            client.settings.save_to_file()
            print(f"LED duration set to: {args.led_duration * 10} ms")
            changed = True
        else:
            print("LED duration must be 1-100 (10-1000 ms)")
    
    # Alarm 1 (CPS) settings
    if hasattr(args, 'alarm1') and args.alarm1 is not None:
        from raysid.protocol import CMD_SET_ALARM1_ENABLED
        await client.send_command(CMD_SET_ALARM1_ENABLED, bytes([1 if args.alarm1 else 0]))
        client.settings.alarm1_enabled = args.alarm1
        client.settings.save_to_file()
        print(f"CPS alarm {'enabled' if args.alarm1 else 'disabled'}")
        changed = True
    
    if hasattr(args, 'alarm1_threshold') and args.alarm1_threshold is not None:
        if 0 <= args.alarm1_threshold <= 65535:
            from raysid.protocol import CMD_SET_ALARM1_THRESHOLD
            await client.send_command(CMD_SET_ALARM1_THRESHOLD, bytes([args.alarm1_threshold // 256, args.alarm1_threshold % 256]))
            client.settings.alarm1_threshold = args.alarm1_threshold
            client.settings.save_to_file()
            print(f"CPS alarm threshold set to: {args.alarm1_threshold} CPS")
            changed = True
        else:
            print("CPS threshold must be 0-65535")
    
    # Alarm 2 (Dose Rate) settings
    if hasattr(args, 'alarm2') and args.alarm2 is not None:
        from raysid.protocol import CMD_SET_ALARM2_ENABLED
        await client.send_command(CMD_SET_ALARM2_ENABLED, bytes([1 if args.alarm2 else 0]))
        client.settings.alarm2_enabled = args.alarm2
        client.settings.save_to_file()
        print(f"Dose rate alarm {'enabled' if args.alarm2 else 'disabled'}")
        changed = True
    
    if hasattr(args, 'alarm2_threshold') and args.alarm2_threshold is not None:
        value = int(args.alarm2_threshold * 100)  # Convert uSv/h to device units
        if 0 <= value <= 65535:
            from raysid.protocol import CMD_SET_ALARM2_THRESHOLD
            await client.send_command(CMD_SET_ALARM2_THRESHOLD, bytes([value // 256, value % 256]))
            client.settings.alarm2_threshold = value
            client.settings.save_to_file()
            print(f"Dose rate alarm threshold set to: {args.alarm2_threshold:.2f} uSv/h")
            changed = True
        else:
            print("Dose rate threshold must be 0-655.35 uSv/h")
    
    if args.clicks is not None:
        client.settings.clicks_enabled = args.clicks
        client.settings.save_to_file()
        print(f"Python clicks {'enabled' if args.clicks else 'disabled'}")
        changed = True
    
    if args.clicks_scale is not None:
        client.settings.clicks_scale = args.clicks_scale
        if client.tick_generator:
            client.tick_generator.tick_scale = args.clicks_scale
        client.settings.save_to_file()
        print(f"Clicks scale set to: 1:{args.clicks_scale}")
        changed = True
    
    if args.units is not None:
        if args.units in ["uSv/h", "mR/h", "uR/h"]:
            client.settings.dose_rate_units = args.units
            client.settings.save_to_file()
            print(f"Dose rate units set to: {args.units}")
            changed = True
        else:
            print("Units must be: uSv/h, mR/h, or uR/h")
    
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

