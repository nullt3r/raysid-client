"""
Textual TUI for live RaySID monitoring — the main interface.

Data-terminal aesthetic: green-on-black, multi-panel layout with
measurement, live spectrum histogram, scrolling packet log, wide CPS
sparkline, and device + RX diagnostics. Launched by bare `raysid`
or `raysid tui`.

Requires the `tui` extra: `pip install -e ".[tui]"`.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Deque, Optional

from . import background as bgmod
from . import calibration, nuclides
from .device_settings import SETTING_GROUPS

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container
    from textual.reactive import reactive
    from textual.screen import Screen
    from textual.widgets import Footer, OptionList, Sparkline, Static
    from textual.widgets.option_list import Option
    TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    TEXTUAL_AVAILABLE = False

if TYPE_CHECKING:
    from .client import RaySIDClient
    from .models import MeasurementData


SPARKLINE_LEN = 240
PACKET_LOG_LEN = 200  # buffer; panel shows as many as fit

# Seconds between device spectrum syncs. Each sync takes 5-20s and
# briefly pauses measurement updates while spectrum packets stream.
SPECTRUM_SYNC_INTERVAL = 45.0
SPECTRUM_SYNC_INITIAL_DELAY = 15.0


# Names for the packet log. Keep short — column is narrow.
PACKET_NAMES = {
    0x01: "spec.stats",
    0x02: "status",
    0x06: "dose",
    0x0A: "settings*",
    0x11: "version*",
    0x17: "meas",
    0x1B: "settings",
    0x1F: "version",
    0x20: "status*",
    0x27: "calib*",
    0x2B: "limits",
    0x30: "spectrum",
    0x31: "spectrum",
    0x32: "spectrum",
    0x57: "calib",
    0x5C: "battery",
}


def is_tui_available() -> bool:
    return TEXTUAL_AVAILABLE


if TEXTUAL_AVAILABLE:

    class Panel(Static):
        """Bordered titled panel with reactive body text."""

        body: reactive[str] = reactive("")

        def __init__(self, title: str, **kwargs) -> None:
            super().__init__(**kwargs)
            self.border_title = title

        def render(self) -> str:
            return self.body

    class MeasurementPanel(Panel):
        cps: reactive[float] = reactive(0.0)
        cpm: reactive[float] = reactive(0.0)
        dose_rate: reactive[float] = reactive(0.0)
        dose_total: reactive[float] = reactive(0.0)

        def render(self) -> str:
            return (
                f"[b green]CPS    [/b green] [b]{self.cps:>10.2f}[/b]\n"
                f"[b green]CPM    [/b green] [b]{self.cpm:>10.1f}[/b]\n"
                f"[b green]µSv/h  [/b green] [b]{self.dose_rate:>10.4f}[/b]\n"
                f"[b green]Dose   [/b green] [b]{self.dose_total:>9.3f}[/b] µSv"
            )

    class SpectrumPanel(Panel):
        DEFAULT_CSS = "SpectrumPanel { content-align: left top; }"

        channels: reactive[tuple] = reactive(tuple())
        # Per-channel keV values (non-linear calibration) — pushed in by
        # the refresher whenever range or channelThAvg changes.
        kev_axis: reactive[tuple] = reactive(tuple())
        # Detected peaks as (channel, keV) — from nuclides.detect_peaks
        peaks: reactive[tuple] = reactive(tuple())
        # Background reference scaled to foreground time (F7 net view)
        bg_scaled: reactive[tuple] = reactive(tuple())
        bg_ratio: reactive[float] = reactive(0.0)
        net_mode: reactive[bool] = reactive(False)
        counts: reactive[int] = reactive(0)
        real_time: reactive[float] = reactive(0.0)
        status: reactive[str] = reactive("waiting for first sync…")

        def _net_bins(self, width: int):
            """Net bins + per-bin significance colors for the NET view."""
            fg_bins = _downsample(self.channels, width)
            bg_bins = _downsample(self.bg_scaled, width)
            r = self.bg_ratio
            var_factor = r * (1.0 + r) if r > 0 else 1.0
            bins = []
            colors = []
            for f, b in zip(fg_bins, bg_bins):
                net = f - b
                bins.append(max(0.0, net))
                bg_raw = b / r if r > 0 else b
                z = net / math.sqrt(max(bg_raw, 1.0) * var_factor)
                colors.append(_z_color(z))
            return bins, colors

        def render(self) -> str:
            width = max(20, self.size.width - 4)
            chart_h = max(3, self.size.height - 6)

            if self.kev_axis:
                range_label = f"{int(self.kev_axis[0])}-{int(self.kev_axis[-1])} keV"
            else:
                range_label = "keV range unknown"

            net_active = (self.net_mode and self.bg_scaled
                          and len(self.bg_scaled) == len(self.channels))
            if net_active:
                bins, colors = self._net_bins(width)
                mode_tag = "[b black on $warning] NET [/] "
            else:
                bins = _downsample(self.channels, width)
                colors = ["#2e8b2e"] * len(bins)
                mode_tag = ""

            if not bins or max(bins) <= 0 or self.real_time <= 0:
                empty = "\n".join(" " * width for _ in range(chart_h + 1))
                return (
                    f"{mode_tag}[dim]{range_label}   counts -   t -[/dim]\n"
                    f"{empty}\n"
                    f"[dim]{self.status}[/dim]"
                )

            peak_top = max(bins) or 1
            levels = chart_h * 8
            heights = [min(levels, int(v * levels / peak_top)) for v in bins]
            blocks = " ▁▂▃▄▅▆▇█"

            rows = []
            for row in range(chart_h):
                from_bottom = chart_h - 1 - row
                line = []
                for col, h in enumerate(heights):
                    full_below = from_bottom * 8
                    in_row = max(0, min(8, h - full_below))
                    if in_row > 0:
                        line.append(f"[{colors[col]}]{blocks[in_row]}[/]")
                    else:
                        line.append(" ")
                rows.append("".join(line))

            tick_line, label_line = _energy_axis(self.kev_axis, width)
            peak_line = _peak_markers(self.peaks, width)

            header = (
                f"{mode_tag}[dim]{range_label}[/dim]   "
                f"counts [b]{self.counts:>10,}[/b]   "
                f"t [b]{self.real_time:>6.0f}[/b]s"
            )
            return (
                f"{header}\n"
                + "\n".join(rows) + "\n"
                + f"[$warning]{peak_line}[/]\n"
                + f"[dim]{tick_line}[/dim]\n"
                + f"[dim]{label_line}[/dim]\n"
                + f"[dim]{self.status}[/dim]"
            )

    class PacketsPanel(Panel):
        lines: reactive[tuple] = reactive(tuple())

        def render(self) -> str:
            if not self.lines:
                return "[dim]waiting…[/dim]"
            return "\n".join(self.lines)

    class SparklinePanel(Container):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.border_title = "CPS HISTORY"

    class SourceIdPanel(Panel):
        """Probable sources — nuclide identification against the app's
        reference library (nuclides.py) + the Currie background verdict."""

        matches: reactive[tuple] = reactive(tuple())
        available: reactive[bool] = reactive(False)
        verdict: reactive[str] = reactive("")
        bg_used: reactive[bool] = reactive(False)

        def render(self) -> str:
            lines = []
            if self.verdict:
                lines.append(self.verdict)
            if not self.available:
                lines.append(
                    "[dim]no reference library\n"
                    "import it from the official app:\n"
                    "raysid library --import <apk>[/dim]"
                )
                return "\n".join(lines)
            if not self.matches:
                lines.append("[dim]waiting for spectrum (≥100 counts)…[/dim]")
                return "\n".join(lines)
            max_rows = max(1, self.size.height - 2 - (1 if self.verdict else 0))
            for i, (name, lib, sim, matched, total) in enumerate(
                    self.matches[:max_rows]):
                style = "b" if i == 0 else "dim"
                lines.append(
                    f"[{style}]{sim:5.1f}[/]  {name:<20.20} "
                    f"[dim]{lib:<6.6} {matched}/{total}p[/dim]"
                )
            if self.bg_used:
                lines.append("[dim]· background-subtracted[/dim]")
            return "\n".join(lines)

    class DevicePanel(Panel):
        fw: reactive[str] = reactive("—")
        dev_id: reactive[str] = reactive("—")
        battery: reactive[str] = reactive("N/A")
        temp: reactive[str] = reactive("N/A")
        charging: reactive[bool] = reactive(False)

        def render(self) -> str:
            chg = "[b yellow]⚡ charging[/b yellow]" if self.charging else "[dim]on battery[/dim]"
            return (
                f"FW   [b]{self.fw}[/b]   ID [dim]{self.dev_id}[/dim]\n"
                f"Bat  [b]{self.battery}[/b]   Temp [b]{self.temp}[/b]\n"
                f"{chg}"
            )

    class RxPanel(Panel):
        rx_bytes: reactive[int] = reactive(0)
        packets: reactive[int] = reactive(0)
        discarded: reactive[int] = reactive(0)
        discard_rate: reactive[float] = reactive(0.0)
        buffer: reactive[int] = reactive(0)

        def render(self) -> str:
            def _kb(n):
                return f"{n/1024:.1f} KB" if n >= 1024 else f"{n} B"
            warn = "red" if self.discard_rate > 1.0 else "green"
            return (
                f"rx   [b]{_kb(self.rx_bytes):>10}[/b]\n"
                f"pkt  [b]{self.packets:>10,}[/b]\n"
                f"drop [b {warn}]{self.discarded:>6,} {self.discard_rate:>5.2f}%[/]\n"
                f"buf  [b]{self.buffer:>10}[/b] B"
            )

    class SettingsScreen(Screen):
        """Full-screen device settings menu, mirroring the official app.

        Built on Textual's OptionList: keyboard and mouse navigation,
        theme-consistent highlight. Every change is sent to the device
        immediately (same command the app sends) and persisted to the
        local settings file.
        """

        BINDINGS = [
            Binding("escape", "app.pop_screen", "Back"),
            Binding("f10", "app.pop_screen", show=False),
            Binding("s", "app.pop_screen", show=False),
            Binding("left", "change(-1)", "Value -"),
            Binding("right", "change(1)", "Value +"),
            Binding("h", "change(-1)", show=False),
            Binding("l", "change(1)", show=False),
        ]

        CSS = """
        SettingsScreen { background: black; }
        #set-title { dock: top; height: 1; padding: 0 2;
                     background: $success; color: black; text-style: bold; }
        #set-list { border: round $success; background: black;
                    color: $success; height: 1fr; margin: 1 1 0 1; }
        #set-list > .option-list--option-highlighted {
            background: $success; color: black; text-style: bold; }
        #set-list:focus > .option-list--option-highlighted {
            background: $success; color: black; text-style: bold; }
        #set-list > .option-list--option-disabled {
            color: $success-darken-1; text-style: bold; }
        #set-hint { height: 3; padding: 0 2; color: $success; }
        """

        def __init__(self, client: "RaySIDClient") -> None:
            super().__init__()
            self.client = client
            # Parallel to option indices: SettingDef, or None for headers
            self._rows = []
            self._status = ""

        def compose(self) -> ComposeResult:
            yield Static(" DEVICE SETTINGS ", id="set-title")
            self._list = OptionList(id="set-list")
            yield self._list
            self._hint = Static(id="set-hint")
            yield self._hint
            yield Footer()

        def on_mount(self) -> None:
            self._populate()
            self._list.focus()

        def _populate(self, highlight: int = None) -> None:
            """(Re)build the option list from current settings values."""
            settings = self.client.settings
            self._rows = []
            options = []
            for group, defs in SETTING_GROUPS:
                options.append(Option(f"  {group}", disabled=True))
                self._rows.append(None)
                for sdef in defs:
                    value_label = sdef.display(getattr(settings, sdef.key))
                    options.append(Option(
                        f"    {sdef.label:<26}{value_label:>42}  ",
                        id=sdef.key,
                    ))
                    self._rows.append(sdef)
            self._list.clear_options()
            self._list.add_options(options)
            if highlight is not None:
                self._list.highlighted = highlight
            elif self._list.option_count:
                # First real setting row
                self._list.highlighted = 1

        def _sdef_at(self, index) -> "object":
            if index is None or not (0 <= index < len(self._rows)):
                return None
            return self._rows[index]

        def on_option_list_option_highlighted(
                self, event: "OptionList.OptionHighlighted") -> None:
            sdef = self._sdef_at(event.option_index)
            if sdef is not None:
                options = " · ".join(sdef.display(v) for v in sdef.values())
                self._hint.update(
                    f"[b]{sdef.label}[/b] — [dim]{options}[/dim]\n"
                    f"[dim]{self._status}[/dim]"
                )

        def on_option_list_option_selected(
                self, event: "OptionList.OptionSelected") -> None:
            # Enter or mouse click on a row: cycle to the next value
            self._cycle(event.option_index, 1)

        def action_change(self, delta: int) -> None:
            self._cycle(self._list.highlighted, delta)

        def _cycle(self, index, delta: int) -> None:
            sdef = self._sdef_at(index)
            if sdef is None:
                return
            values = sdef.values()
            current = getattr(self.client.settings, sdef.key)
            pos = values.index(current) if current in values else 0
            new_value = values[(pos + delta) % len(values)]
            # Optimistic UI update; apply_setting persists after sending
            setattr(self.client.settings, sdef.key, new_value)
            self._populate(highlight=index)
            self._set_status(f"{sdef.label}: sending…", index)
            self.run_worker(self._send(sdef, new_value), exclusive=False)

        def _set_status(self, text: str, index) -> None:
            self._status = text
            sdef = self._sdef_at(index)
            if sdef is not None:
                options = " · ".join(sdef.display(v) for v in sdef.values())
                self._hint.update(
                    f"[b]{sdef.label}[/b] — [dim]{options}[/dim]\n{text}"
                )

        async def _send(self, sdef, value) -> None:
            index = self._list.highlighted
            try:
                ok = await self.client.apply_setting(sdef.key, value)
                if ok:
                    status = f"{sdef.label}: sent 0x{sdef.command:02X} to device · saved"
                    if (sdef.key == "tick_on_client" and value
                            and self.client.tick_generator is None):
                        status = (f"[red]{sdef.label}: audio not available — "
                                  f"pip install -e \".[audio]\"[/red]")
                    self._set_status(status, index)
                else:
                    self._set_status(f"[red]{sdef.label}: rejected[/red]", index)
            except Exception as e:
                self._set_status(f"[red]{sdef.label}: send failed: {e}[/red]", index)


    class MonitorApp(App):
        """Data-terminal style live monitor."""

        CSS = """
        Screen { background: black; color: $success; layout: vertical; }

        #title { dock: top; height: 1; padding: 0 2; background: $success;
                 color: black; text-style: bold; }

        #top { layout: grid; grid-size: 3 1; grid-gutter: 1;
               grid-columns: 1fr 3fr 1fr;
               height: 2fr; min-height: 9; margin: 1 1 0 1; }
        #mid    { height: 3fr; min-height: 8; margin: 0 1; }
        #bot { layout: grid; grid-size: 3 1; grid-gutter: 1;
               grid-columns: 2fr 1fr 2fr;
               height: 1fr; min-height: 6; margin: 0 1 1 1; }

        Panel { border: round $success; padding: 0 1; color: $success;
                background: black; height: 100%; }
        Panel:focus { border: round $warning; }

        SparklinePanel { border: round $success; padding: 0 1; height: 100%;
                         color: $success; background: black; }

        Sparkline { height: 1fr; min-height: 4; }
        Sparkline > .sparkline--max-color { color: $warning; }
        Sparkline > .sparkline--min-color { color: $success-darken-2; }
        """

        BINDINGS = [
            # Total-Commander-style function keys, shown in the Footer
            # (clickable); plain digits and letters work as aliases.
            Binding("f2", "show_settings", "Settings"),
            Binding("2", "show_settings", show=False),
            Binding("s", "show_settings", show=False),
            Binding("f5", "sync_now", "Sync now"),
            Binding("5", "sync_now", show=False),
            Binding("f6", "save_background", "Save bkg"),
            Binding("6", "save_background", show=False),
            Binding("b", "save_background", show=False),
            Binding("f7", "toggle_net", "Net view"),
            Binding("7", "toggle_net", show=False),
            Binding("n", "toggle_net", show=False),
            Binding("f8", "clear_spectrum", "Clear spectrum"),
            Binding("8", "clear_spectrum", show=False),
            Binding("x", "clear_spectrum", show=False),
            Binding("f9", "reset_stats", "RX reset"),
            Binding("9", "reset_stats", show=False),
            Binding("r", "reset_stats", show=False),
            Binding("c", "clear_log", "Clear log"),
            Binding("f10", "quit", "Quit"),
            Binding("0", "quit", show=False),
            Binding("q", "quit", show=False),
            Binding("ctrl+c", "quit", show=False),
        ]

        def __init__(self, client: "RaySIDClient", duration: float = 0.0,
                     on_measurement=None) -> None:
            super().__init__()
            self.client = client
            self.duration = duration
            self._on_measurement = on_measurement
            self._history: Deque[float] = deque([0.0] * SPARKLINE_LEN, maxlen=SPARKLINE_LEN)
            self._packet_log: Deque[str] = deque(maxlen=PACKET_LOG_LEN)
            self._kev_axis_key = None
            # Set by F5 to trigger an immediate spectrum sync
            self._sync_request: Optional[object] = None
            # Nuclide reference library (loaded lazily per energy range)
            self._library = None
            self._library_range = None
            # Background reference (loaded lazily per energy range)
            self._bg = None
            self._bg_range = None

        def compose(self) -> ComposeResult:
            yield Static(
                " RaySID  │  unofficial BLE client  │  q quit  r reset  c clear",
                id="title",
            )
            with Container(id="top"):
                yield MeasurementPanel("MEASUREMENT", id="meas")
                yield SpectrumPanel("SPECTRUM", id="spec")
                yield PacketsPanel("PACKETS", id="pkts")
            with Container(id="mid"):
                spark_panel = SparklinePanel(id="spark_panel")
                yield spark_panel
            with Container(id="bot"):
                yield DevicePanel("DEVICE", id="dev")
                yield RxPanel("RX", id="rx")
                yield SourceIdPanel("SOURCE ID", id="srcid")
            yield Footer()

        async def on_mount(self) -> None:
            self._spark = Sparkline(list(self._history), summary_function=max)
            await self.query_one("#spark_panel", SparklinePanel).mount(self._spark)
            self.client.on_packet = self._on_packet
            self.set_interval(1.0, self._refresh_device_and_rx)
            self.set_interval(0.5, self._refresh_spectrum)
            self.run_worker(self._run_monitor(), exclusive=True)
            self.run_worker(self._spectrum_sync_loop(), exclusive=False)

        async def _run_monitor(self) -> None:
            try:
                await self.client.monitor(duration=self.duration, callback=self._on_data)
            finally:
                self.exit()

        async def _spectrum_sync_loop(self) -> None:
            """Periodically download accumulated spectrum from the device.

            Runs alongside live monitoring. Each sync takes 5-20s and
            briefly pauses measurement updates while the device streams
            spectrum packets. F5 wakes the loop for an immediate sync.
            """
            import asyncio
            self._sync_request = asyncio.Event()
            # Initial delay: let measurement get going first
            await self._sleep_or_sync_request(SPECTRUM_SYNC_INITIAL_DELAY)
            while True:
                sp = self.query_one("#spec", SpectrumPanel)
                sp.status = "syncing…"
                try:
                    spectrum = await self.client.sync_spectrum()
                    if spectrum:
                        ts = datetime.now().strftime("%H:%M:%S")
                        sp.status = f"last sync {ts}"
                        self._identify_sources(spectrum)
                    else:
                        sp.status = "[red]sync failed[/red]"
                except Exception as e:
                    sp.status = f"[red]sync error: {e}[/red]"
                await self._sleep_or_sync_request(SPECTRUM_SYNC_INTERVAL)

        def _get_background(self, energy_range: int):
            if self._bg is None or self._bg_range != energy_range:
                self._bg_range = energy_range
                self._bg = bgmod.load_background(energy_range)
            return self._bg

        def _identify_sources(self, spectrum) -> None:
            """Background verdict + peak detection + library matching on a
            synced spectrum (same code path as `raysid spectrum`)."""
            panel = self.query_one("#srcid", SourceIdPanel)
            sp = self.query_one("#spec", SpectrumPanel)

            # Background / Currie verdict + data for the NET view
            bg = self._get_background(spectrum.energy_range)
            if bg is not None and spectrum.real_time > 0:
                net = bgmod.compute_net(spectrum.channels, spectrum.real_time, bg)
                sp.bg_scaled = tuple(c * net.ratio for c in bg.channels)
                sp.bg_ratio = net.ratio
                if net.detected:
                    panel.verdict = (
                        f"[b black on $warning] DETECTED [/] "
                        f"net {net.net_cps:+.2f} CPS  z={net.z_total:.1f}"
                    )
                else:
                    panel.verdict = (
                        f"[dim]no source above background  "
                        f"z={net.z_total:.1f} (L_C 95%)[/dim]"
                    )
            else:
                sp.bg_scaled = tuple()
                panel.verdict = "[dim]no background ref — F6 to save one[/dim]"
            panel.bg_used = bg is not None

            if self._library is None or self._library_range != spectrum.energy_range:
                self._library_range = spectrum.energy_range
                self._library = nuclides.load_library(spectrum.energy_range)
            panel.available = bool(self._library)
            if not self._library:
                return

            peaks = nuclides.detect_peaks(
                spectrum.channels, spectrum.real_time, spectrum.total_counts,
                spectrum.energy_range, spectrum.channel_th_avg)
            matches = nuclides.identify(
                spectrum.channels, spectrum.real_time, spectrum.total_counts,
                spectrum.energy_range, spectrum.channel_th_avg,
                self._library, detected=peaks, bg=bg)

            sp.peaks = tuple((p.center, p.center_kev) for p in peaks)
            panel.matches = tuple(
                (m.source.name, m.source.lib, m.similarity,
                 m.matched_peaks, m.total_lib_peaks)
                for m in matches[:8]
            )

        def action_save_background(self) -> None:
            """F6: store the currently displayed spectrum as the background
            reference for its energy range."""
            sd = self.client.spectrum_data
            sp = self.query_one("#spec", SpectrumPanel)
            if self.client.spectrum_acquiring:
                sp.status = "[$warning]sync in progress — wait for it to finish, then press F6[/]"
                return
            if not sd.real_time or sum(sd.channels) <= 0:
                sp.status = "[$warning]no synced spectrum to save as background[/]"
                return
            try:
                bgmod.save_background(sd)
            except Exception as e:
                sp.status = f"[red]background save failed: {e}[/red]"
                return
            self._bg = None  # force reload
            sp.status = (
                f"background saved: {sum(sd.channels):,.0f} cts / "
                f"{sd.real_time:.0f}s — clear spectrum (F8) before measuring samples"
            )
            self._identify_sources(sd)

        def action_toggle_net(self) -> None:
            """F7: toggle the background-subtracted NET spectrum view."""
            sp = self.query_one("#spec", SpectrumPanel)
            bg = self._get_background(self.client.spectrum_data.energy_range)
            if bg is None:
                sp.status = "[$warning]no background reference — save one with F6 first[/]"
                sp.net_mode = False
                return
            sp.net_mode = not sp.net_mode
            sp.status = ("NET view: spectrum minus background, colored by significance"
                         if sp.net_mode else "gross spectrum view")

        async def _sleep_or_sync_request(self, timeout: float) -> None:
            """Sleep up to `timeout` seconds; wake early on F5 sync request."""
            import asyncio
            try:
                await asyncio.wait_for(self._sync_request.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._sync_request.clear()

        def action_sync_now(self) -> None:
            if self._sync_request is not None and not self._sync_request.is_set():
                self._sync_request.set()
                self.query_one("#spec", SpectrumPanel).status = "sync requested…"

        def _on_data(self, data: "MeasurementData") -> None:
            self._history.append(max(0.0, data.cps))

            mp = self.query_one("#meas", MeasurementPanel)
            mp.cps = data.cps
            mp.cpm = data.cpm
            mp.dose_rate = data.dose_rate_usv
            mp.dose_total = self.client.dose_data.dose_usv

            self._spark.data = list(self._history)

            if self._on_measurement is not None:
                try:
                    self._on_measurement(data)
                except Exception:
                    pass

        def _on_packet(self, ptype: int, length: int) -> None:
            name = PACKET_NAMES.get(ptype, f"0x{ptype:02X}")
            ts = datetime.now().strftime("%H:%M:%S")
            self._packet_log.appendleft(f"[dim]{ts}[/dim] 0x{ptype:02X} [b]{name:<10}[/b] {length:>3}B")
            try:
                self.query_one("#pkts", PacketsPanel).lines = tuple(self._packet_log)
            except Exception:
                pass

        def _refresh_device_and_rx(self) -> None:
            info = self.client.device_info
            dev = self.query_one("#dev", DevicePanel)
            dev.fw = f"v{info.firmware_version}" if info.firmware_version else "—"
            dev.dev_id = info.device_id or "—"
            dev.battery = f"{info.battery_percent}%" if info.battery_percent > 0 else "N/A"
            dev.temp = f"{info.temperature_celsius:.1f}°C" if info.temperature_celsius > -50 else "N/A"
            dev.charging = info.is_charging

            stats = self.client.get_rx_stats()
            rx = self.query_one("#rx", RxPanel)
            rx.rx_bytes = stats["rx_bytes"]
            rx.packets = stats["packets_valid"]
            rx.discarded = stats["bytes_discarded"]
            rx.discard_rate = stats["discard_rate"]
            rx.buffer = stats["buffer_current"]

        def _refresh_spectrum(self) -> None:
            sd = self.client.spectrum_data
            sp = self.query_one("#spec", SpectrumPanel)

            # Rebuild the keV axis only when the calibration inputs move.
            th_avg = self.client.device_info.channel_th_avg or calibration.DEFAULT_CHANNEL_TH_AVG
            axis_key = (sd.energy_range, round(th_avg, 1))
            if axis_key != self._kev_axis_key:
                self._kev_axis_key = axis_key
                sp.kev_axis = tuple(calibration.energy_axis(sd.energy_range, th_avg))

            sp.counts = sd.total_counts or sum(sd.channels)
            sp.real_time = sd.real_time or 0.0
            sp.channels = tuple(sd.channels)

        def action_show_settings(self) -> None:
            self.push_screen(SettingsScreen(self.client))

        def action_reset_stats(self) -> None:
            self.client.reset_rx_stats()

        def action_clear_log(self) -> None:
            self._packet_log.clear()
            self.query_one("#pkts", PacketsPanel).lines = tuple()

        def action_clear_spectrum(self) -> None:
            """Wipe the accumulated spectrum on the device (same as
            double-tap of the physical button)."""
            self.run_worker(self._clear_spectrum(), exclusive=False)

        async def _clear_spectrum(self) -> None:
            sp = self.query_one("#spec", SpectrumPanel)
            sp.status = "clearing device spectrum…"
            try:
                await self.client.clear_spectrum(
                    self.client.settings.spectrum_energy_range
                )
                ts = datetime.now().strftime("%H:%M:%S")
                sp.status = f"cleared at {ts}"
            except Exception as e:
                sp.status = f"[red]clear failed: {e}[/red]"


def _z_color(z: float) -> str:
    """Significance shading for the NET view (per displayed bin)."""
    if z < 2.0:
        return "#1f5f1f"    # within background noise
    if z < 3.5:
        return "#2e8b2e"    # mild excess
    if z < 5.0:
        return "#d4a017"    # significant
    return "#c41e3a"        # strong


def _peak_markers(peaks, width: int) -> str:
    """Line with ▲keV markers at detected-peak columns."""
    row = [" "] * width
    for channel, kev in peaks:
        col = int(round(channel / (nuclides.SPECTRUM_SIZE - 1) * (width - 1)))
        label = f"▲{int(kev)}"
        start = max(0, min(width - len(label), col))
        if all(row[start + j] == " " for j in range(len(label))):
            for j, ch in enumerate(label):
                row[start + j] = ch
    return "".join(row)


def _downsample(channels, width: int) -> list:
    """Sum-downsample a channel array into `width` bins."""
    n = len(channels)
    if n == 0 or width <= 0:
        return []
    chunk = max(1, n // width)
    return [sum(channels[i:i + chunk]) for i in range(0, chunk * width, chunk)]


def _energy_axis(kev_axis, width: int) -> tuple:
    """Build (tick_line, label_line) for an energy axis below the chart.

    `kev_axis` is the per-channel keV list (non-linear calibration);
    tick columns are placed where the calibration actually puts each
    energy, not at linear fractions of the range.
    """
    if width < 10 or len(kev_axis) < 2:
        return " " * width, " " * width
    min_kev, max_kev = kev_axis[0], kev_axis[-1]
    # Pick "nice" tick values
    span = max_kev - min_kev
    if span <= 1200:
        step = 200
    elif span <= 2500:
        step = 500
    else:
        step = 1000
    ticks = []
    k = int(min_kev // step) * step
    while k <= max_kev:
        if k >= min_kev:
            ticks.append(k)
        k += step
    if not ticks or ticks[0] > min_kev + step / 4:
        ticks.insert(0, int(min_kev))
    if ticks[-1] < int(max_kev):
        ticks.append(int(max_kev))

    n = len(kev_axis)
    tick_line = [" "] * width
    label_line = [" "] * width
    for kev in ticks:
        channel = bisect_left(kev_axis, kev)
        col = int(round(channel / (n - 1) * (width - 1)))
        col = max(0, min(width - 1, col))
        tick_line[col] = "│"
        label = f"{int(kev)}"
        start = max(0, min(width - len(label), col - len(label) // 2))
        for i, ch in enumerate(label):
            if start + i < width:
                label_line[start + i] = ch
    return "".join(tick_line), "".join(label_line)


async def run_monitor_tui(client: "RaySIDClient", duration: float = 0.0,
                          on_measurement=None) -> None:
    """Launch the Textual monitor dashboard. Blocks until quit or duration elapses."""
    if not TEXTUAL_AVAILABLE:
        raise RuntimeError(
            "TUI mode requires the `textual` package. Install with: pip install -e \".[tui]\""
        )
    client.reset_rx_stats()
    app = MonitorApp(client, duration=duration, on_measurement=on_measurement)

    # Console log handlers would write straight into the Textual display —
    # detach them while the TUI owns the terminal. File handlers keep
    # logging; a root NullHandler stops third-party libs (bleak) from
    # falling back to stderr via logging.lastResort.
    raysid_logger = logging.getLogger('raysid')
    console_handlers = [
        h for h in raysid_logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    for h in console_handlers:
        raysid_logger.removeHandler(h)
    root_logger = logging.getLogger()
    null_handler = logging.NullHandler()
    root_logger.addHandler(null_handler)
    try:
        await app.run_async()
    finally:
        root_logger.removeHandler(null_handler)
        for h in console_handlers:
            raysid_logger.addHandler(h)
        client.on_packet = None
