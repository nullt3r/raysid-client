"""
Energy calibration for RaySID spectra.

Ported from the official app (SpectrumUtils.channel2kevOld and
recalculateEnergyCalibration, decompiled from v1.3.1e). The device does
NOT send an energy calibration; the app computes keV per channel from
built-in anchor tables parameterized by `channelThAvg` — the running
average of the Cs-137 (661.7 keV) peak position channel, which the
device reports in every status packet (type 0x02). 847 is the nominal
value when the device hasn't reported one yet.

Energy-range codes (note: NOT the ordering the old constants implied):
    8 → ~8-1000 keV   (app calibration files use prefix "1k-")
    4 → ~15-2100 keV  (prefix "2k-")
    2 → ~20-3500 keV  (prefix "3k-")

channel_to_kev() is a verbatim port of the app's anchor scan, matching
its output bit-for-bit over channels 40-1800 (including its quirks for
out-of-nominal channelThAvg values, where anchors can overlap). The one
deliberate difference: the app maps channels below 40 (under the
hardware threshold) to 0 keV; we pin them to the range's starting
energy — the value the app itself anchors channel 0 to when building
its display axis — so the axis stays monotonic.
"""

from __future__ import annotations

from typing import List, Tuple

DEFAULT_CHANNEL_TH_AVG = 847.0
SPECTRUM_CHANNELS = 1800

# Starting energies the app anchors channel 0 to, per range code
# (recalculateEnergyCalibration).
RANGE_START_KEV = {8: 8.0, 4: 15.0, 2: 20.0}


def _anchor_tables(energy_range: int, channel_th_avg: float) -> Tuple[List[float], List[float]]:
    """(keV[], channel[]) anchor tables in the app's array order.

    `f` is the Cs-137 peak channel; range 8 offsets anchors from the
    nominal 847, ranges 4 and 2 scale anchor channels by f directly.
    """
    f = channel_th_avg if channel_th_avg > 0 else DEFAULT_CHANNEL_TH_AVG

    if energy_range == 4:
        kev = [15.0, 26.3, 32.0, 59.6, 238.6, 338.3,
               583.2, 661.7, 911.2, 1460.8, 1609.0, 2100.0]
        ch = [40.0, 0.1126 * f, 100.0, 0.2023 * f, 0.6318 * f, 0.8376 * f,
              1.2091 * f, 1.2749 * f, 1.5094 * f, 1.9279 * f, 2.0007 * f, 1800.0]
    elif energy_range == 2:
        kev = [20.0, 32.0, 59.6, 238.6, 338.3, 583.2,
               661.7, 930.0, 1460.8, 1609.0, 2614.5, 3500.0]
        ch = [40.0, 100.0, 0.1556 * f, 0.378 * f, 0.4929 * f, 0.7386 * f,
              0.7995 * f, 0.9892 * f, 1.2847 * f, 1.3399 * f, 1.6559 * f, 1800.0]
    else:  # 8, and the app forces invalid codes to 8
        d = 847.0 - f
        kev = [8.0, 32.0, 59.5, 93.0, 185.7, 239.0, 295.0, 338.0,
               352.0, 511.0, 583.0, 609.0, 661.7, 911.0, 1000.0]
        ch = [40.0, 100.0,
              223.0 - 0.21 * d, 357.0 - 0.53 * d, 685.0 - 0.927 * d,
              847.0 - 1.0 * d, 992.0 - 0.995 * d, 1081.0 - 0.97 * d,
              1120.0 - 0.95 * d, 1381.0 - 0.6 * d, 1454.0 - 0.39 * d,
              1474.0 - 0.29 * d, 1500.0, 1700.0, 1800.0]
    return kev, ch


def _scan(channel: float, energy_range: int,
          kev: List[float], ch: List[float]) -> float:
    """channel2kevOld() body: first anchor with channel >= requested,
    linear interpolation from the previous one."""
    if channel < 40.0:
        return RANGE_START_KEV[energy_range]
    if channel > 1800.0:
        return 0.0
    for k in range(1, len(ch)):
        if channel <= ch[k]:
            return kev[k - 1] + ((kev[k] - kev[k - 1])
                                 * (channel - ch[k - 1]) / (ch[k] - ch[k - 1]))
    return 0.0


def channel_to_kev(channel: float, energy_range: int,
                   channel_th_avg: float = DEFAULT_CHANNEL_TH_AVG) -> float:
    """Energy in keV at the given channel (verbatim channel2kevOld port)."""
    if energy_range not in (2, 4, 8):
        energy_range = 8
    kev, ch = _anchor_tables(energy_range, channel_th_avg)
    return _scan(channel, energy_range, kev, ch)


def energy_axis(energy_range: int,
                channel_th_avg: float = DEFAULT_CHANNEL_TH_AVG,
                n_channels: int = SPECTRUM_CHANNELS) -> List[float]:
    """Per-channel keV values for the full spectrum."""
    if energy_range not in (2, 4, 8):
        energy_range = 8
    kev, ch = _anchor_tables(energy_range, channel_th_avg)
    return [_scan(c, energy_range, kev, ch) for c in range(n_channels)]


def kev_to_channel(kev: float, energy_range: int,
                   channel_th_avg: float = DEFAULT_CHANNEL_TH_AVG) -> int:
    """Nearest channel for the given energy.

    Port of the app's kev2channel(): binary search over channel_to_kev.
    """
    lo = 0
    hi = SPECTRUM_CHANNELS - 1
    mid = hi // 2
    for _ in range(15):
        if mid in (lo, hi):
            break
        if kev < channel_to_kev(mid, energy_range, channel_th_avg):
            hi = mid
        else:
            lo = mid
        mid = (lo + hi) // 2
        if hi - lo <= 1:
            lo_kev = channel_to_kev(lo, energy_range, channel_th_avg)
            hi_kev = channel_to_kev(hi, energy_range, channel_th_avg)
            return lo if kev - lo_kev < hi_kev - kev else hi
    return mid
