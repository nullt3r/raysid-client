"""
Background reference spectra and net-spectrum statistics.

The core tool for telling a sample apart from ambient background:

1. A measured background spectrum is stored per energy range in
   ~/.raysid_background/ (capture it with `raysid background --measure`,
   `raysid background --save`, or F6 in the TUI).

2. net = S − B·r with r = T_s/T_b. Counts are Poisson, so the null-
   hypothesis variance of the net counts is B·r·(1+r); z-scores follow.

3. Detection decision per Currie (1968) / ISO 11929: critical level
   L_C = k·sqrt(B·r·(1+r)) with k = 1.645 for 95 % confidence. Net
   counts above L_C mean a statistically real detection; the detection
   limit L_D = k² + 2·L_C says what the measurement could have seen.

The decision statistic is computed on totals, where the normal
approximation of the Poisson holds; per-channel/per-bin significance
for display is derived by consumers from `net` and the background.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .logging_config import get_logger

logger = get_logger('background')

BACKGROUND_DIR = Path(os.path.expanduser("~/.raysid_background"))

# Same folder prefixes as the calibration files / nuclide library
RANGE_PREFIX = {8: "1k", 4: "2k", 2: "3k"}

# Currie k for 95 % confidence (one-sided)
CURRIE_K = 1.645


@dataclass
class BackgroundSpectrum:
    """A stored background reference measurement."""

    channels: List[float]
    real_time: float
    total_counts: float
    energy_range: int
    channel_th_avg: float = 847.0
    timestamp: float = 0.0
    device_id: str = ""

    @property
    def counts_sum(self) -> float:
        return float(sum(self.channels))

    @property
    def cps(self) -> float:
        return self.counts_sum / self.real_time if self.real_time > 0 else 0.0

    @property
    def age_hours(self) -> float:
        return (time.time() - self.timestamp) / 3600.0 if self.timestamp else 0.0


def _path_for(energy_range: int, base_dir: Optional[Path] = None) -> Path:
    base_dir = Path(base_dir) if base_dir else BACKGROUND_DIR
    prefix = RANGE_PREFIX.get(energy_range, "1k")
    return Path(base_dir) / f"background-{prefix}.json"


def save_background(spectrum, base_dir: Optional[Path] = None) -> Path:
    """Persist a SpectrumData as the background reference for its range."""
    if not spectrum.real_time or spectrum.real_time <= 0:
        raise ValueError("background spectrum has no accumulation time")
    if sum(spectrum.channels) <= 0:
        raise ValueError("background spectrum is empty")
    base_dir = Path(base_dir) if base_dir else BACKGROUND_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    path = _path_for(spectrum.energy_range, base_dir)
    payload = {
        "channels": list(spectrum.channels),
        "real_time": spectrum.real_time,
        "total_counts": spectrum.total_counts or sum(spectrum.channels),
        "energy_range": spectrum.energy_range,
        "channel_th_avg": getattr(spectrum, "channel_th_avg", 847.0),
        "timestamp": time.time(),
        "device_id": getattr(spectrum, "device_id", ""),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)
    logger.info(f"Background saved: {path} ({payload['total_counts']:.0f} counts, "
                f"{spectrum.real_time:.0f}s)")
    return path


def load_background(energy_range: int,
                    base_dir: Optional[Path] = None) -> Optional[BackgroundSpectrum]:
    path = _path_for(energy_range, base_dir)
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text())
        return BackgroundSpectrum(
            channels=[float(c) for c in d["channels"]],
            real_time=float(d["real_time"]),
            total_counts=float(d.get("total_counts", 0)),
            energy_range=int(d.get("energy_range", energy_range)),
            channel_th_avg=float(d.get("channel_th_avg", 847.0)),
            timestamp=float(d.get("timestamp", 0)),
            device_id=d.get("device_id", ""),
        )
    except Exception as e:
        logger.warning(f"Could not load background {path}: {e}")
        return None


def delete_background(energy_range: int, base_dir: Optional[Path] = None) -> bool:
    path = _path_for(energy_range, base_dir)
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Net spectrum + Currie decision
# ---------------------------------------------------------------------------

@dataclass
class NetResult:
    """Background-subtracted spectrum with detection statistics."""

    net: List[float]          # per-channel net counts (can be negative)
    ratio: float              # T_s / T_b time scaling applied to background

    net_total: float          # total net counts
    sigma0_total: float       # null-hypothesis sigma of net_total
    z_total: float            # net_total / sigma0_total
    l_c: float                # Currie critical level (counts)
    l_d: float                # Currie detection limit (counts)
    detected: bool            # net_total > l_c

    net_cps: float            # net count rate
    confidence: float         # one-sided confidence used for L_C


def compute_net(fg_channels: Sequence[float], fg_time: float,
                bg: BackgroundSpectrum, k: float = CURRIE_K) -> NetResult:
    """Background-subtract a foreground spectrum and apply the Currie test.

    fg and bg must be from the same energy range; the caller is expected
    to check that (energy range changes device binning entirely).
    """
    if fg_time <= 0 or bg.real_time <= 0:
        raise ValueError("both spectra need accumulation time")
    r = fg_time / bg.real_time
    n = min(len(fg_channels), len(bg.channels))

    net: List[float] = []
    var_factor = r * (1.0 + r)
    for i in range(n):
        net.append(fg_channels[i] - bg.channels[i] * r)

    s_total = float(sum(fg_channels[:n]))
    b_total = float(sum(bg.channels[:n]))
    net_total = s_total - b_total * r
    sigma0 = math.sqrt(max(b_total, 1.0) * var_factor)
    l_c = k * sigma0
    l_d = k * k + 2.0 * l_c
    z_total = net_total / sigma0 if sigma0 > 0 else 0.0

    return NetResult(
        net=net, ratio=r,
        net_total=net_total, sigma0_total=sigma0, z_total=z_total,
        l_c=l_c, l_d=l_d, detected=net_total > l_c,
        net_cps=net_total / fg_time,
        confidence=0.95 if abs(k - CURRIE_K) < 1e-9 else float("nan"),
    )


def verdict_line(net: NetResult) -> str:
    """One-line human-readable Currie verdict."""
    if net.detected:
        return (f"SOURCE DETECTED  net {net.net_total:+,.0f} cts "
                f"({net.net_cps:+.2f} CPS)  z={net.z_total:.1f}  "
                f"L_C={net.l_c:,.0f} (95%)")
    return (f"no source above background  net {net.net_total:+,.0f} cts  "
            f"z={net.z_total:.1f}  L_C={net.l_c:,.0f} (95%)")
