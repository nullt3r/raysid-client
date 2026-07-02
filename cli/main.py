"""
RaySID CLI - Command-line interface for RaySID radiation detector.

Usage:
    raysid [command] [options]      # no command launches the TUI dashboard

Commands:
    tui        - Full-screen dashboard (the main interface, also the default)
    monitor    - Live radiation monitoring (plain console)
    spectrum   - Download a gamma spectrum + source identification
    background - Measure / manage the background reference
    library    - Import / show the nuclide reference library
    settings   - View and modify device settings
    info       - Show device information
    dose       - Dose accumulation display
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional

import click

from raysid import RaySIDClient
from raysid.models import DeviceSettings
from raysid.audio import is_audio_available
from raysid.logging_config import setup_logging, get_logger

from .commands import (
    cmd_tui,
    cmd_monitor,
    cmd_spectrum,
    cmd_background,
    cmd_info,
    cmd_settings,
    cmd_dose,
)

logger = get_logger('cli')


# Shared options decorator - add to each command
def add_common_options(f):
    """Add common connection/debug options to a command."""
    f = click.option('--address', '-a', help='Device MAC address (skip scan)')(f)
    f = click.option('--verbose', '-v', is_flag=True, help='Verbose output')(f)
    f = click.option('--debug', is_flag=True, help='Debug packet tracing')(f)
    f = click.option('--quiet', '-q', is_flag=True, help='Suppress non-essential output')(f)
    f = click.option('--log-file', help='Write logs to file')(f)
    return f


def add_sound_options(f):
    """Add sound options to commands that support it."""
    f = click.option('--sound', '-s', is_flag=True, help='Enable Geiger counter tick sounds')(f)
    f = click.option('--tick-scale', type=int, default=None,
                     help='Clicks scale ratio 1:N (default: saved clicks_scale setting)')(f)
    f = click.option('--tick-style', type=click.Choice(['1', '2', '3', '4', '5']),
                     default='3', help='Tick style: 1=sharp, 2=medium, 3=DP-5, 4=deep, 5=thump')(f)
    return f


class Options:
    """Container for CLI options."""
    pass


async def run_command(command_func, opts: Options, apply_settings: bool = True,
                      persistent: bool = False, **kwargs):
    """Run a command with proper setup and teardown.

    Args:
        apply_settings: push the locally stored device settings to the
            device after connecting (like the official app). Read-only
            commands pass False so they don't silently reconfigure the
            device.
        persistent: keep reconnecting forever (monitor/TUI); one-shot
            commands give up after a few attempts instead of hanging.
    """
    # Print banner
    if not opts.quiet:
        click.echo()
        click.echo("=" * 60)
        click.echo("  RaySID BLE Client by @nullt3r (unofficial)")
        click.echo("=" * 60)

    # Setup logging
    setup_logging(
        verbose=opts.verbose,
        debug=opts.debug,
        log_file=opts.log_file,
        quiet=opts.quiet
    )

    # Create client
    client = RaySIDClient(
        verbose=opts.verbose,
        debug_packets=opts.debug,
        max_reconnect_attempts=None if persistent else 5,
    )
    if opts.address:
        client.target_address = opts.address

    # Geiger click sounds: the --sound flag, or the persisted
    # tick_on_client setting ("Sound on client" in the settings menu)
    explicit_sound = getattr(opts, 'sound', False)
    enable_sound = explicit_sound or client.settings.tick_on_client
    if enable_sound and not is_audio_available():
        if explicit_sound:
            logger.warning("Sound requested but numpy/sounddevice not available")
            click.echo("  Install with: pip install numpy sounddevice")
        enable_sound = False
    if enable_sound:
        from raysid.audio import TickSoundGenerator
        client.tick_generator = TickSoundGenerator()
        # CLI flag wins; otherwise the persisted clicks_scale setting
        scale = getattr(opts, 'tick_scale', None) or client.settings.clicks_scale
        style = getattr(opts, 'tick_style', '3')
        client.tick_generator.tick_scale = scale
        client.tick_generator.set_tick_style(style)
        logger.info(f"Sound enabled (scale 1:{scale}, style: {style})")

    try:
        # Connect
        if not await client.scan_and_connect():
            return 1

        await client.initialize(apply_settings=apply_settings)

        # Run command
        await command_func(client, opts, **kwargs)
        return 0
        
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted")
        return 130
    except Exception as e:
        logger.error(f"Error: {e}")
        if opts.debug:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        if client.tick_generator:
            client.tick_generator.stop()
        await client.disconnect()


# ==================== CLI GROUP ====================

@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """RaySID BLE Client - Radiation detector interface.

    Running `raysid` with no command launches the full-screen TUI
    dashboard (the main interface).

    \b
    Examples:
        raysid                       # TUI dashboard
        raysid monitor --sound       # Plain console monitoring
        raysid spectrum --csv        # Download spectrum, save CSV
        raysid settings --show
        raysid info

    Run 'raysid <command> --help' for command-specific options.
    """
    if ctx.invoked_subcommand is None:
        from raysid.tui import is_tui_available
        if is_tui_available():
            ctx.invoke(tui)
        else:
            click.echo(ctx.get_help())
            click.echo("\nTip: install the TUI dashboard with `pip install -e \".[tui]\"`"
                       " and run plain `raysid` to launch it.")


# ==================== TUI ====================

@cli.command()
@add_common_options
@add_sound_options
@click.option('--duration', '-d', type=float, default=0, help='Duration in seconds (0=indefinite)')
@click.option('--output', '-o', help='Output filename for measurement log')
@click.option('--csv', is_flag=True, help='Log measurements to CSV file')
@click.option('--json', 'save_json', is_flag=True, help='Log measurements to JSON file')
def tui(address, verbose, debug, quiet, log_file,
        sound, tick_scale, tick_style,
        duration, output, csv, save_json):
    """Full-screen dashboard — the main interface.

    Live measurement, spectrum, packet log, device diagnostics and the
    device settings menu (F2). Also launched by running `raysid` with
    no command.
    """
    from raysid.tui import is_tui_available
    if not is_tui_available():
        click.echo("The TUI requires the `textual` package.")
        click.echo("Install with: pip install -e \".[tui]\"")
        sys.exit(1)

    opts = Options()
    opts.address = address
    opts.verbose = verbose
    opts.debug = debug
    opts.quiet = quiet
    opts.log_file = log_file
    opts.sound = sound
    opts.tick_scale = tick_scale
    opts.tick_style = tick_style

    class Args:
        pass
    args = Args()
    args.duration = duration
    args.output = output
    args.csv = csv
    args.json = save_json

    sys.exit(asyncio.run(run_command(cmd_tui, opts, persistent=True, args=args)))


# ==================== MONITOR ====================

@cli.command()
@add_common_options
@add_sound_options
@click.option('--duration', '-d', type=float, default=0, help='Duration in seconds (0=indefinite)')
@click.option('--output', '-o', help='Output filename')
@click.option('--csv', is_flag=True, help='Save measurements to CSV file')
@click.option('--json', 'save_json', is_flag=True, help='Save measurements to JSON file')
def monitor(address, verbose, debug, quiet, log_file,
            sound, tick_scale, tick_style,
            duration, output, csv, save_json):
    """Live radiation monitoring (plain console output).

    \b
    Examples:
        raysid monitor                     # Continuous monitoring
        raysid monitor --sound             # With Geiger clicks
        raysid monitor -d 60 --csv         # 60 seconds, save to CSV
        raysid monitor --csv -o data.csv   # Save to specific file
    """
    opts = Options()
    opts.address = address
    opts.verbose = verbose
    opts.debug = debug
    opts.quiet = quiet
    opts.log_file = log_file
    opts.sound = sound
    opts.tick_scale = tick_scale
    opts.tick_style = tick_style

    class Args:
        pass
    args = Args()
    args.duration = duration
    args.output = output
    args.csv = csv
    args.json = save_json
    args.verbose = verbose

    sys.exit(asyncio.run(run_command(cmd_monitor, opts, persistent=True, args=args)))


# ==================== SPECTRUM ====================

@cli.command()
@add_common_options
@click.option('--sync', is_flag=True, help='Download current device spectrum (default action)')
@click.option('--clear', is_flag=True, help='Clear device spectrum first')
@click.option('--output', '-o', help='Output base filename')
@click.option('--csv', is_flag=True, help='Save CSV output')
@click.option('--json', 'save_json', is_flag=True, help='Save JSON output')
def spectrum(address, verbose, debug, quiet, log_file,
             sync, clear, output, csv, save_json):
    """Download the device's accumulated gamma spectrum.

    \b
    Examples:
        raysid spectrum                    # Download and show summary
        raysid spectrum --csv --json      # Download and save data files
        raysid spectrum --clear            # Clear device spectrum (restart accumulation)
        raysid spectrum --clear --sync     # Clear, then download fresh
    """
    opts = Options()
    opts.address = address
    opts.verbose = verbose
    opts.debug = debug
    opts.quiet = quiet
    opts.log_file = log_file
    opts.sound = False

    class Args:
        pass
    args = Args()
    # Sync is the default action; --clear alone only clears.
    args.sync = sync or not clear
    args.clear = clear
    args.output = output
    args.csv = csv
    args.json = save_json

    sys.exit(asyncio.run(run_command(cmd_spectrum, opts, args=args)))


# ==================== INFO ====================

@cli.command()
@add_common_options
def info(address, verbose, debug, quiet, log_file):
    """Show device information.
    
    \b
    Examples:
        raysid info
        raysid info --verbose
    """
    opts = Options()
    opts.address = address
    opts.verbose = verbose
    opts.debug = debug
    opts.quiet = quiet
    opts.log_file = log_file
    opts.sound = False
    
    class Args:
        pass
    args = Args()
    
    sys.exit(asyncio.run(run_command(cmd_info, opts, apply_settings=False, args=args)))


# ==================== DOSE ====================

@cli.command()
@add_common_options
@click.option('--reset', '-r', is_flag=True, help='Reset dose counter')
def dose(address, verbose, debug, quiet, log_file, reset):
    """Dose accumulation display.
    
    \b
    Examples:
        raysid dose
        raysid dose --reset
    """
    opts = Options()
    opts.address = address
    opts.verbose = verbose
    opts.debug = debug
    opts.quiet = quiet
    opts.log_file = log_file
    opts.sound = False
    
    class Args:
        pass
    args = Args()
    args.reset = reset
    
    sys.exit(asyncio.run(run_command(cmd_dose, opts, apply_settings=False, args=args)))


# ==================== SETTINGS ====================

@cli.command()
@add_common_options
@click.option('--show', is_flag=True, help='Show all current settings')
@click.option('--sensitivity', type=click.Choice(['fast', 'normal', 'accurate', 'very-accurate']),
              help='Device sensitivity mode')
@click.option('--energy-range', type=click.Choice(['2', '4', '8']),
              help='Energy range: 8=~8-1000keV (narrow), 4=~15-2100keV, 2=~20-3500keV (wide)')
@click.option('--channels', type=click.Choice(['200', '600', '1800', 'auto']),
              help='Spectrum channels')
@click.option('--ticks', type=click.Choice(['on', 'off']), help='Device tick sounds')
@click.option('--tick-sound', type=click.Choice(['on', 'off']), help='Tick speaker sound')
@click.option('--tick-led', type=click.Choice(['on', 'off']), help='LED flash on tick')
@click.option('--clicks', type=click.Choice(['on', 'off']),
              help='Client plays Geiger clicks (persistent tick_on_client setting)')
@click.option('--clicks-scale', type=click.Choice(['1', '5', '10', '20', '50', '100', '250']),
              help='Python clicks scale 1:N')
@click.option('--set', 'set_pairs', multiple=True, metavar='KEY=VALUE',
              help='Set any device setting by key (keys shown by --show; '
                   'e.g. --set alarm1_threshold=100 --set ticks_scale=20)')
def settings(address, verbose, debug, quiet, log_file,
             show, sensitivity, energy_range, channels, ticks, tick_sound,
             tick_led, clicks, clicks_scale, set_pairs):
    """View and modify device settings.

    \b
    Examples:
        raysid settings --show
        raysid settings --sensitivity accurate
        raysid settings --energy-range 4 --channels 1800
        raysid settings --set alarm1_threshold=100 --set alarm1_enabled=on
        raysid settings --clicks on --clicks-scale 10
    """
    opts = Options()
    opts.address = address
    opts.verbose = verbose
    opts.debug = debug
    opts.quiet = quiet
    opts.log_file = log_file
    opts.sound = False
    
    class Args:
        pass
    args = Args()
    args.show = show
    args.sensitivity = sensitivity
    args.energy_range = int(energy_range) if energy_range else None
    args.channels = (0 if channels == 'auto' else int(channels)) if channels else None
    args.ticks = ticks == 'on' if ticks else None
    args.tick_sound = tick_sound == 'on' if tick_sound else None
    args.tick_led = tick_led == 'on' if tick_led else None
    args.clicks = clicks == 'on' if clicks else None
    args.clicks_scale = int(clicks_scale) if clicks_scale else None
    args.set_pairs = tuple(p.split('=', 1) for p in set_pairs if '=' in p)

    sys.exit(asyncio.run(run_command(cmd_settings, opts, args=args)))


# ==================== BACKGROUND ====================

@cli.command()
@add_common_options
@click.option('--measure', type=int, metavar='SECONDS',
              help='Clear the device spectrum, accumulate background for '
                   'SECONDS, then save it as the reference')
@click.option('--save', 'save_current', is_flag=True,
              help="Save the device's currently accumulated spectrum as the "
                   'background reference')
@click.option('--delete', 'delete_range', type=click.Choice(['2', '4', '8']),
              help='Delete the stored background for an energy range')
def background(address, verbose, debug, quiet, log_file,
               measure, save_current, delete_range):
    """Manage background reference spectra (for net-spectrum statistics).

    A stored background enables background subtraction: the Currie
    detection verdict in `raysid spectrum` and the TUI, the net-spectrum
    view (F7), and background-subtracted source identification.

    \b
    Examples:
        raysid background                  # Show status
        raysid background --measure 600    # Measure a fresh 10-minute background
        raysid background --save           # Save current device spectrum as background
    """
    from raysid import background as bgmod

    if delete_range:
        code = int(delete_range)
        if bgmod.delete_background(code):
            click.echo(f"Deleted background for energy range {code}.")
        else:
            click.echo(f"No stored background for energy range {code}.")
        return

    if measure or save_current:
        opts = Options()
        opts.address = address
        opts.verbose = verbose
        opts.debug = debug
        opts.quiet = quiet
        opts.log_file = log_file
        opts.sound = False

        class Args:
            pass
        args = Args()
        args.measure = measure
        sys.exit(asyncio.run(run_command(cmd_background, opts, args=args)))

    # Status (offline)
    click.echo("\nBackground reference status:")
    found = False
    for code, prefix in sorted(bgmod.RANGE_PREFIX.items(), reverse=True):
        bg = bgmod.load_background(code)
        if bg:
            found = True
            click.echo(f"  {prefix} (energy range {code}): "
                       f"{bg.counts_sum:,.0f} cts / {bg.real_time:.0f}s "
                       f"({bg.cps:.2f} CPS), {bg.age_hours:.1f}h old")
        else:
            click.echo(f"  {prefix} (energy range {code}): not measured")
    if not found:
        click.echo("\nMeasure one with: raysid background --measure 600")


# ==================== LIBRARY ====================

@cli.command()
@click.option('--import', 'import_path', type=click.Path(exists=True),
              help='Import nuclide reference libraries from your copy of the '
                   'official app (.apk or .xapk file)')
def library(import_path):
    """Manage the nuclide reference library for source identification.

    The reference spectra are proprietary data of the official app and
    are not bundled with this client. Import them once from your own
    copy of the app package; they are stored in ~/.raysid_library/.

    \b
    Examples:
        raysid library                              # Show status
        raysid library --import Raysid_1.3.1.xapk   # Import from app package
    """
    from raysid import nuclides

    if import_path:
        try:
            imported = nuclides.import_library(Path(import_path))
        except Exception as e:
            click.echo(f"Import failed: {e}")
            sys.exit(1)
        if imported:
            click.echo(f"Imported: {', '.join(sorted(imported))} -> {nuclides.LIBRARY_DIR}")
        else:
            click.echo("No raysid library assets found in that package.")
            sys.exit(1)

    click.echo("\nNuclide reference library status:")
    any_found = False
    for code, prefix in sorted(nuclides.RANGE_PREFIX.items(), reverse=True):
        sources = nuclides.load_library(code)
        state = f"{len(sources)} sources" if sources else "not imported"
        any_found = any_found or bool(sources)
        click.echo(f"  {prefix} (energy range {code}): {state}")
    if not any_found and not import_path:
        click.echo("\nImport with: raysid library --import <apk/xapk>")


# ==================== ENTRY POINT ====================

def main():
    """Entry point."""
    cli()


if __name__ == '__main__':
    main()
