"""
RaySID CLI - Command-line interface for RaySID radiation detector.

Usage:
    raysid <command> [options]
    
Commands:
    monitor   - Live radiation monitoring (default)
    spectrum  - Acquire/download gamma spectrum
    settings  - View and modify device settings
    info      - Show device information
    dose      - Dose accumulation display
    json      - Output current reading as JSON
"""

import asyncio
import sys
from typing import Optional

import click

from raysid import RaySIDClient
from raysid.models import DeviceSettings
from raysid.audio import is_audio_available
from raysid.logging_config import setup_logging, get_logger

from .commands import (
    cmd_monitor,
    cmd_spectrum,
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
    f = click.option('--tick-scale', type=int, default=20,
                     help='Clicks scale ratio 1:N (default: 20)')(f)
    f = click.option('--tick-style', type=click.Choice(['1', '2', '3', '4', '5']),
                     default='3', help='Tick style: 1=sharp, 2=medium, 3=DP-5, 4=deep, 5=thump')(f)
    return f


class Options:
    """Container for CLI options."""
    pass


async def run_command(command_func, opts: Options, **kwargs):
    """Run a command with proper setup and teardown."""
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
    
    # Check sound availability
    enable_sound = getattr(opts, 'sound', False)
    if enable_sound:
        temp_settings = DeviceSettings.load_from_file()
        enable_sound = opts.sound or temp_settings.clicks_enabled
        
        if enable_sound and not is_audio_available():
            logger.warning("Sound requested but numpy/sounddevice not available")
            click.echo("  Install with: pip install numpy sounddevice")
            enable_sound = False
    
    # Create client
    client = RaySIDClient(
        verbose=opts.verbose,
        enable_sound=False,
        debug_packets=opts.debug
    )
    if opts.address:
        client.target_address = opts.address
    
    # Configure tick sound
    if enable_sound:
        from raysid.audio import TickSoundGenerator
        client.tick_generator = TickSoundGenerator()
        scale = getattr(opts, 'tick_scale', 20)
        style = getattr(opts, 'tick_style', '3')
        client.tick_generator.tick_scale = scale
        client.tick_generator.set_tick_style(style)
        logger.info(f"Sound enabled (scale 1:{scale}, style: {style})")
    
    try:
        # Connect
        if not await client.scan_and_connect():
            return 1
        
        # Initialize (always full sync, preserve spectrum)
        await client.initialize()
        
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
    
    \b
    Usage:
        raysid <command> [options]
    
    \b
    Examples:
        raysid monitor --sound
        raysid spectrum --sync --graph
        raysid settings --show
        raysid info --verbose
    
    Run 'raysid <command> --help' for command-specific options.
    """
    if ctx.invoked_subcommand is None:
        # Default to monitor with minimal options
        click.echo(ctx.get_help())


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
    """Live radiation monitoring.
    
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
    
    sys.exit(asyncio.run(run_command(cmd_monitor, opts, args=args)))


# ==================== SPECTRUM ====================

@cli.command()
@add_common_options
@click.option('--sync', is_flag=True, help='Download current device spectrum')
@click.option('--clear', is_flag=True, help='Clear device spectrum first')
@click.option('--output', '-o', help='Output base filename')
@click.option('--timeout', '-t', type=float, default=60, help='Acquisition timeout in seconds')
@click.option('--graph', is_flag=True, help='Save PNG graph')
@click.option('--csv', is_flag=True, help='Save CSV output')
@click.option('--json', 'save_json', is_flag=True, help='Save JSON output')
@click.option('--log-scale', is_flag=True, help='Use logarithmic Y scale')
@click.option('--no-peaks', is_flag=True, help="Don't mark peaks on graph")
@click.option('--title', help='Custom title for graph')
@click.option('--interval', '-i', type=int, help='Continuous mode: save every N seconds')
@click.option('--duration', '-d', type=int, default=0, help='Duration for continuous mode')
@click.option('--delta', type=int, help='Delta mode: background time in seconds')
def spectrum(address, verbose, debug, quiet, log_file,
             sync, clear, output, timeout, graph, csv, save_json,
             log_scale, no_peaks, title, interval, duration, delta):
    """Acquire or download gamma spectrum.
    
    \b
    Examples:
        raysid spectrum --sync --graph      # Download and graph current spectrum
        raysid spectrum -t 120 --graph      # Acquire for 2 minutes, then graph
        raysid spectrum --clear --sync      # Clear device, download fresh
        raysid spectrum --delta 60 --graph  # Delta mode with background subtraction
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
    args.sync = sync
    args.clear = clear
    args.output = output
    args.timeout = timeout
    args.graph = graph
    args.csv = csv
    args.json = save_json
    args.log_scale = log_scale
    args.no_peaks = no_peaks
    args.title = title
    args.interval = interval
    args.duration = duration
    args.delta = delta
    
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
    
    sys.exit(asyncio.run(run_command(cmd_info, opts, args=args)))


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
    
    sys.exit(asyncio.run(run_command(cmd_dose, opts, args=args)))


# ==================== SETTINGS ====================

@cli.command()
@add_common_options
@click.option('--show', is_flag=True, help='Show all current settings')
@click.option('--sensitivity', type=click.Choice(['fast', 'normal', 'accurate', 'very-accurate']),
              help='Device sensitivity mode')
@click.option('--energy-range', type=click.Choice(['2', '4', '8']),
              help='Energy range: 2=45-2000keV, 4=45-3500keV, 8=45-7500keV')
@click.option('--channels', type=click.Choice(['600', '1200', '1800']),
              help='Spectrum channels')
@click.option('--ticks', type=click.Choice(['on', 'off']), help='Device tick sounds')
@click.option('--tick-sound', type=click.Choice(['on', 'off']), help='Tick speaker sound')
@click.option('--tick-led', type=click.Choice(['on', 'off']), help='LED flash on tick')
@click.option('--clicks', type=click.Choice(['on', 'off']), help='Python Geiger sounds')
@click.option('--clicks-scale', type=click.Choice(['1', '5', '10', '20', '50', '100', '250']),
              help='Python clicks scale 1:N')
@click.option('--units', type=click.Choice(['uSv/h', 'mR/h', 'uR/h']), help='Dose rate units')
def settings(address, verbose, debug, quiet, log_file,
             show, sensitivity, energy_range, channels, ticks, tick_sound,
             tick_led, clicks, clicks_scale, units):
    """View and modify device settings.
    
    \b
    Examples:
        raysid settings --show
        raysid settings --sensitivity accurate
        raysid settings --energy-range 4 --channels 1800
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
    args.channels = int(channels) if channels else None
    args.ticks = ticks == 'on' if ticks else None
    args.tick_sound = tick_sound == 'on' if tick_sound else None
    args.tick_led = tick_led == 'on' if tick_led else None
    args.clicks = clicks == 'on' if clicks else None
    args.clicks_scale = int(clicks_scale) if clicks_scale else None
    args.units = units
    args.tick_duration = None
    args.led_duration = None
    args.alarm1 = None
    args.alarm1_threshold = None
    args.alarm2 = None
    args.alarm2_threshold = None
    
    sys.exit(asyncio.run(run_command(cmd_settings, opts, args=args)))


# ==================== JSON ====================

# ==================== ENTRY POINT ====================

def main():
    """Entry point."""
    cli()


if __name__ == '__main__':
    main()
