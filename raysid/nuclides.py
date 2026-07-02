"""
Nuclide identification — port of the official app's detection pipeline.

Ported verbatim from the decompiled app (v1.3.1e):
- Tab2.detectPeaks()            → detect_peaks()
- Tab2.detectPeakInInterval()   → _detect_peak_in_interval()
- Source.applyCurrentCalibration() → ReferenceSource.resample()
- Source.calculateSimilarityCompare3() + the peak-match scoring in
  Tab2.compareSpectrumWithLibrary() → identify()

The reference library is the app's own: per-energy-range sets of N42
spectra with a custom <DHS:Raysid><Peaks> annotation block, bundled in
the APK as assets/{1k,2k,3k}-raysid-lib.zip. The library is proprietary
app data and is NOT shipped with this client — import it from your own
copy of the app with `raysid library --import <apk/xapk>`; it is
unpacked into ~/.raysid_library/.

Matching gate as in the app: identification only runs once the spectrum
has at least 100 counts.
"""

from __future__ import annotations

import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import calibration
from .logging_config import get_logger

logger = get_logger('nuclides')

LIBRARY_DIR = Path(os.path.expanduser("~/.raysid_library"))

# Library folder prefix per energy-range code (same as calibration files)
RANGE_PREFIX = {8: "1k", 4: "2k", 2: "3k"}

SPECTRUM_SIZE = 1800

# Minimum total counts before identification runs (app gate)
MIN_TICKS_FOR_ID = 100.0


def _jdiv(a: float, b: float) -> float:
    """Java float division semantics: x/0 = ±inf, 0/0 = NaN."""
    if b != 0.0:
        return a / b
    if a > 0.0:
        return math.inf
    if a < 0.0:
        return -math.inf
    return math.nan


# ---------------------------------------------------------------------------
# Peak detection (Tab2.detectPeaks)
# ---------------------------------------------------------------------------

@dataclass
class DetectedPeak:
    center: int = -1        # channel
    beg: int = -1
    end: int = -1
    width: int = -1
    height: float = -1.0    # on the smoothed spectrum
    height_proc: float = -1.0
    center_kev: int = -1
    integral: float = 0.0   # raw counts above baseline
    cps: float = 0.0
    fwhm: float = -1.0


def _smooth(src: Sequence[float], depth: int) -> List[float]:
    """One pass of the app's running-window boxcar (verbatim edge logic)."""
    size = len(src)
    out: List[float] = []
    y = 0.0
    last_window = 0
    for i in range(size):
        win = (i * 2 if i < depth else (size - i - 1) * 2) + 1
        if win > depth:
            win = depth
        half = (win - 1) // 2
        hi = i + half
        if hi + 1 < size:
            y += src[hi]
        if last_window < depth and i < depth and i > 0:
            y += src[hi - 1]
        lo = i - half
        if lo - 1 >= 0:
            y -= src[lo - 1]
        if i >= (size - depth // 2) + 1:
            y -= src[lo - 2]
        out.append(y / win)
        last_window = win
    return out


def _smooth_depth(energy_range: int, ticks_actual: float) -> int:
    if energy_range == 4:
        depth = round(25.0 / math.sqrt(ticks_actual / 2000.0) + 20.0)
    elif energy_range == 2:
        depth = round(20.0 / math.sqrt(ticks_actual / 2000.0) + 15.0)
    else:  # 8 (default in the app)
        depth = round(35.0 / math.sqrt(ticks_actual / 2000.0) + 25.0)
    depth = max(15, min(121, depth))
    if depth % 2 == 0:
        depth += 1
    return depth


def _detect_peak_in_interval(beg: int, end: int, smooth2: Sequence[float],
                             raw: Sequence[float], ticks_actual: float,
                             ticks_compensated: float, live_time: float,
                             kev_axis: Sequence[float]) -> Optional[DetectedPeak]:
    """Port of Tab2.detectPeakInInterval (baseline-subtracted peak in a valley
    interval of the twice-smoothed spectrum)."""
    i3 = max(0, beg)
    i4 = min(len(smooth2) - 1 if end >= len(smooth2) else end, end)
    if i4 > SPECTRUM_SIZE:
        i4 = SPECTRUM_SIZE
    if i4 == i3:
        return None

    v_beg = smooth2[i3]
    v_end = smooth2[i4]
    area_above = 0.0     # f — smoothed counts above the linear baseline
    total_above = 0.0    # f2 — smoothed counts where above baseline
    integral = 0.0       # y — RAW counts above baseline (from first nonzero)
    first_nonzero = -1   # i7
    best_diff = -1.0     # f3
    best_pos = -1        # i8

    span = i4 - i3
    for i in range(i3, i4):
        v = smooth2[i]
        if first_nonzero == -1 and v > 0.001:
            first_nonzero = i
        frac = (i - i3) / span
        baseline = smooth2[i3] + frac * (smooth2[i4] - smooth2[i3])
        if v > baseline:
            area_above += v - baseline
            total_above += v
        diff = v - baseline
        if best_diff < diff:
            best_pos = i
            best_diff = diff
        if first_nonzero >= 0:
            integral += raw[i] - (((i - i3) * (v_end - v_beg)) / span + v_beg)

    square_proc = _jdiv(10000.0 * area_above, ticks_compensated)

    if i3 == 0:
        # First interval: peak = absolute maximum
        best_diff = -1.0
        best_pos = -1
        for i in range(i3, i4):
            if best_diff < smooth2[i]:
                best_pos = i
                best_diff = smooth2[i]

    if best_pos <= 0 or best_pos <= i3 + 5 or best_pos >= i4 - 5:
        return None

    height_proc = _jdiv(area_above * 100.0, total_above)
    min_height_proc = 15.0 - (ticks_actual * 5.0) / 100000.0
    if min_height_proc < 5.0:
        min_height_proc = 5.0
    if ((height_proc < min_height_proc and best_diff < 50.0)
            or best_diff < 0.3 or square_proc < 5.0):
        return None

    peak = DetectedPeak()
    peak.center = best_pos
    peak.beg = first_nonzero
    peak.end = i4
    peak.width = i4 - first_nonzero
    peak.height = best_diff
    peak.height_proc = height_proc
    peak.center_kev = round(kev_axis[best_pos])
    peak.integral = integral
    peak.cps = _jdiv(integral, live_time)

    # FWHM as % of center energy
    half = smooth2[best_pos] - best_diff / 2.0
    left = best_pos
    i = best_pos
    while i > first_nonzero and smooth2[i] >= half:
        left -= 1
        i -= 1
    right = best_pos
    i = best_pos
    while i < i4 and smooth2[i] >= half:
        right += 1
        i += 1
    if peak.center_kev > 0:
        peak.fwhm = ((round(kev_axis[min(right, SPECTRUM_SIZE - 1)])
                      - round(kev_axis[max(left, 0)])) * 100.0) / peak.center_kev
    return peak


def detect_peaks(channels: Sequence[float], real_time: float,
                 total_counts: float, energy_range: int,
                 channel_th_avg: float) -> List[DetectedPeak]:
    """Port of Tab2.detectPeaks(): triple boxcar smoothing, valley
    segmentation, baseline-subtracted peak candidates with the app's
    statistical filters."""
    size = len(channels)
    if size != SPECTRUM_SIZE:
        return []
    ticks_actual = float(sum(channels))
    if ticks_actual <= 0:
        return []
    ticks_compensated = float(total_counts) if total_counts > 0 else ticks_actual
    live_time = real_time if real_time > 0 else 1.0
    kev_axis = calibration.energy_axis(energy_range, channel_th_avg)

    depth = _smooth_depth(energy_range, ticks_actual)
    s1 = _smooth([float(c) for c in channels], depth)
    s2 = _smooth(s1, depth)
    s3 = _smooth(s2, depth)

    peaks: List[DetectedPeak] = []
    prev_bottom = 0
    max_diff = -1.0
    max_pos = -1
    for i in range(10, size - 10):
        a = _jdiv(s3[i], s3[i - 10])
        b = _jdiv(s3[i + 10], s3[i])
        if a < b:
            diff = _jdiv((b - a) * 100.0, a)
            if max_diff < diff:
                max_pos = i - 1
                max_diff = diff
        else:
            if max_diff > 0.5:
                p = _detect_peak_in_interval(
                    prev_bottom, max_pos, s2, channels, ticks_actual,
                    ticks_compensated, live_time, kev_axis)
                if p is not None and p.center > 0:
                    peaks.append(p)
                prev_bottom = max_pos
            max_diff = -1.0
            max_pos = -1

    p = _detect_peak_in_interval(
        prev_bottom, size - 1, s2, channels, ticks_actual,
        ticks_compensated, live_time, kev_axis)
    if p is not None and p.center > 0:
        peaks.append(p)

    # App filter: a lone peak in the backscatter hump region is noise
    if len(peaks) == 1 and 64 < peaks[0].center_kev < 120:
        peaks = []
    return peaks


# ---------------------------------------------------------------------------
# Reference library (Source / SourceLib / SourceLibs)
# ---------------------------------------------------------------------------

@dataclass
class ReferenceSource:
    name: str
    lib: str                # category folder: IND / MED / NORM / TENORM / Other
    energy_range: int
    kev: List[float]        # source's own per-channel energy calibration
    counts: List[float]     # per-channel counts
    live_time: float
    real_time: float
    peaks: List[Tuple[float, str, float]] = field(default_factory=list)  # (keV, name, cps)

    # Filled by resample():
    data: List[float] = field(default_factory=list)
    ticks_actual: float = 0.0
    _resample_key: tuple = None

    @property
    def all_peaks_cps(self) -> float:
        return sum(cps for _, _, cps in self.peaks)

    def resample(self, kev_axis: Sequence[float]) -> None:
        """Port of Source.applyCurrentCalibration(): re-bin this source's
        spectrum from its own keV grid onto the current one."""
        key = (round(kev_axis[0], 3), round(kev_axis[-1], 3), len(kev_axis))
        if self._resample_key == key:
            return
        new_data: List[float] = []
        ticks = 0.0
        i = 0
        n = len(self.counts)
        for target_kev in kev_axis:
            value = 0.0
            while True:
                if i >= n - 1 or i < 0:
                    break
                x = self.kev[i]
                x2 = self.kev[i + 1]
                if target_kev < x:
                    i -= 1
                    if i < 0:
                        i = 0
                        break
                    continue
                if x <= target_kev <= x2:
                    if x2 != x:
                        value = self.counts[i] + ((target_kev - x) / (x2 - x)) \
                                * (self.counts[i + 1] - self.counts[i])
                        ticks += value
                    break
                # target_kev > x2
                if i + 1 >= n - 1:
                    i = n - 2
                    break
                i = i + 1
            new_data.append(value)
        self.data = new_data
        self.ticks_actual = ticks
        self._resample_key = key


def _parse_iso_duration(text: str) -> float:
    m = re.search(r"PT([\d.]+)S", text or "")
    return float(m.group(1)) if m else 0.0


def parse_n42_reference(path: Path, lib: str, energy_range: int) -> Optional[ReferenceSource]:
    """Parse one library N42 file (regex-based: the files are generated by
    one app and use an undeclared DHS: namespace prefix that breaks XML
    parsers)."""
    try:
        text = Path(path).read_text(errors="replace")
        bounds = re.search(r"<EnergyBoundaryValues>([^<]+)</EnergyBoundaryValues>", text)
        data = re.search(r"<ChannelData[^>]*>([^<]+)</ChannelData>", text)
        if not bounds or not data:
            return None
        kev = [float(v) for v in bounds.group(1).split()]
        counts = [float(v) for v in data.group(1).split()]
        if len(kev) != SPECTRUM_SIZE or len(counts) != SPECTRUM_SIZE:
            logger.debug(f"{path.name}: unexpected sizes kev={len(kev)} counts={len(counts)}")
            return None
        live = _parse_iso_duration(
            (re.search(r"<LiveTimeDuration>([^<]+)", text) or [None, ""])[1])
        real = _parse_iso_duration(
            (re.search(r"<RealTimeDuration>([^<]+)", text) or [None, ""])[1])

        peaks = []
        for pk in re.finditer(
                r"<Peak>\s*<PeakEnergy>([^<]+)</PeakEnergy>\s*<PeakName>([^<]*)</PeakName>"
                r"\s*<PeakCounts>[^<]*</PeakCounts>\s*<PeakCPS>([^<]+)</PeakCPS>", text):
            peaks.append((float(pk.group(1)), pk.group(2), float(pk.group(3))))

        # Source name like the app: strip "1k-" prefix and ".n42" suffix
        stem = path.name
        name = stem[3:-4] if len(stem) > 7 else stem
        return ReferenceSource(
            name=name, lib=lib, energy_range=energy_range,
            kev=kev, counts=counts, live_time=live, real_time=real, peaks=peaks,
        )
    except Exception as e:
        logger.warning(f"Failed to parse {path}: {e}")
        return None


def library_path(energy_range: int, base_dir: Path = LIBRARY_DIR) -> Path:
    prefix = RANGE_PREFIX.get(energy_range, "1k")
    return Path(base_dir) / f"{prefix}-raysid-lib"


def load_library(energy_range: int, base_dir: Path = LIBRARY_DIR) -> List[ReferenceSource]:
    """Load the reference library for an energy range from ~/.raysid_library.

    Returns [] if the library hasn't been imported (see import_library).
    """
    root = library_path(energy_range, base_dir)
    sources: List[ReferenceSource] = []
    if not root.is_dir():
        return sources
    for n42 in sorted(root.rglob("*.n42")):
        src = parse_n42_reference(n42, lib=n42.parent.name, energy_range=energy_range)
        if src is not None:
            sources.append(src)
    logger.info(f"Loaded {len(sources)} reference sources for range {energy_range}")
    return sources


def import_library(app_package: Path, base_dir: Path = LIBRARY_DIR) -> List[str]:
    """Extract the reference libraries from a user-supplied copy of the
    official app (.apk or .xapk) into ~/.raysid_library. Returns the list
    of imported library names."""
    app_package = Path(app_package)
    imported: List[str] = []

    def _extract_libs(apk_zip: zipfile.ZipFile) -> None:
        for info in apk_zip.infolist():
            m = re.fullmatch(r"assets/([123]k-raysid-lib)\.zip", info.filename)
            if not m:
                continue
            lib_name = m.group(1)
            target = Path(base_dir) / lib_name
            target.mkdir(parents=True, exist_ok=True)
            with apk_zip.open(info) as f:
                inner = zipfile.ZipFile(f)
                inner.extractall(target)
            imported.append(lib_name)

    with zipfile.ZipFile(app_package) as z:
        names = z.namelist()
        if any(n.startswith("assets/") for n in names):
            _extract_libs(z)  # plain APK
        else:
            # XAPK: find the inner main APK
            for n in names:
                if n.endswith(".apk") and "config." not in n:
                    with z.open(n) as f:
                        _extract_libs(zipfile.ZipFile(f))
    return imported


# ---------------------------------------------------------------------------
# Identification (calculateSimilarityCompare3 + compareSpectrumWithLibrary)
# ---------------------------------------------------------------------------

@dataclass
class SourceMatch:
    source: ReferenceSource
    similarity: float
    matched_peaks: int
    total_lib_peaks: int
    unmatched_detected: int

    @property
    def label(self) -> str:
        return f"{self.source.name} ({self.source.lib})"


def _shape_similarity(measured: Sequence[float], meas_ticks: float,
                      source: ReferenceSource, fg_live_time: float = 0.0,
                      bg_channels: Optional[Sequence[float]] = None,
                      bg_ticks: float = 0.0,
                      bg_live_time: float = 0.0) -> float:
    """Port of Source.calculateSimilarityCompare3: L1 distance of
    normalized shapes sampled every 5th channel. With a background
    spectrum both the measurement and the reference get the live-time
    scaled background subtracted first (the app's background branch)."""
    if not source.data or meas_ticks <= 0 or source.ticks_actual <= 0:
        return 0.0

    use_bg = bg_channels is not None and len(bg_channels) >= SPECTRUM_SIZE
    if use_bg:
        f6 = bg_live_time + 0.1
        f7 = fg_live_time / f6
        f8 = source.live_time / f6
        meas_denom = meas_ticks - f7 * bg_ticks
        src_denom = source.ticks_actual - f8 * bg_ticks

    total = 0.0
    for k in range(50, SPECTRUM_SIZE - 50, 5):
        if use_bg:
            y = _jdiv(measured[k] - bg_channels[k] * f7, meas_denom)
            y2 = _jdiv(source.data[k] - bg_channels[k] * f8, src_denom)
            d = abs(y - y2)
        else:
            d = abs(measured[k] / meas_ticks - source.data[k] / source.ticks_actual)
        if not (d <= 99999.0):   # NaN-safe clamp (Java: 99999 <= d)
            d = 99999.0
        if d <= 99998.0:
            total += d
    result = total * 500.0
    if result > 99.0:
        result = 99.0
    return float(100.0 - result)


def identify(channels: Sequence[float], real_time: float, total_counts: float,
             energy_range: int, channel_th_avg: float,
             library: List[ReferenceSource],
             detected: Optional[List[DetectedPeak]] = None,
             bg=None) -> List[SourceMatch]:
    """Score every library source against the measured spectrum.

    Port of Tab2.compareSpectrumWithLibrary(): shape similarity from
    calculateSimilarityCompare3, then the peak-match adjustment. Returns
    matches sorted by similarity (best first). Empty when below the
    app's 100-count gate or when the library is empty.

    With `bg` (a background.BackgroundSpectrum of the same energy range)
    the comparison runs on the background-subtracted shapes — the app's
    background branch — which is what makes weak sources identifiable.
    """
    if not library or len(channels) != SPECTRUM_SIZE:
        return []
    meas_ticks = float(sum(channels))
    if meas_ticks < MIN_TICKS_FOR_ID:
        return []

    kev_axis = calibration.energy_axis(energy_range, channel_th_avg)
    if detected is None:
        detected = detect_peaks(channels, real_time, total_counts,
                                energy_range, channel_th_avg)

    bg_channels = bg_ticks = bg_live = None
    if bg is not None and bg.energy_range == energy_range:
        bg_channels = bg.channels
        bg_ticks = float(sum(bg.channels))
        bg_live = bg.real_time

    matches: List[SourceMatch] = []
    for source in library:
        # NOTE: resample() caches its result on the shared ReferenceSource
        # objects — identify() is not safe to run concurrently on the same
        # library list from multiple threads.
        source.resample(kev_axis)
        if bg_channels is not None:
            similarity = _shape_similarity(
                channels, meas_ticks, source, fg_live_time=real_time,
                bg_channels=bg_channels, bg_ticks=bg_ticks, bg_live_time=bg_live)
        else:
            similarity = _shape_similarity(channels, meas_ticks, source)

        matched_count = 0
        if detected:
            # Detected-peak weights: cps * (keV/3000)^0.25
            weights = []
            detected_cps = 0.0
            for dp in detected:
                w = dp.cps * math.pow(dp.center_kev / 3000.0, 0.25)
                weights.append(w)
                detected_cps += w

            matched_sum = 0.0
            all_sum = 0.0
            for kev, _name, cps in source.peaks:
                pw = math.pow(kev / 3000.0, 0.25) * cps
                all_sum += pw
                for j, dp in enumerate(detected):
                    if dp.center_kev * 0.91 < kev < dp.center_kev * 1.1:
                        weights[j] = 0.0
                        matched_count += 1
                        matched_sum += pw
                        break

            unknown_cps = sum(weights)
            f12 = _jdiv(matched_sum, all_sum) if all_sum > 0 else 0.0
            f13 = _jdiv(0.2 * (all_sum - matched_sum), all_sum) if all_sum > 0 else 0.0
            f = unknown_cps / detected_cps if detected_cps > 0 else 0.0

            if source.peaks and all_sum > 0:
                similarity *= 0.9
                score = (f12 - f13 - f) * 10.0
            else:
                score = 0.0

            if source.all_peaks_cps > 0:
                similarity += score
            else:
                similarity += score - len(detected) * 5.0
            if similarity < 0.0:
                similarity = 0.0

        matches.append(SourceMatch(
            source=source, similarity=similarity,
            matched_peaks=matched_count, total_lib_peaks=len(source.peaks),
            unmatched_detected=len(detected) - matched_count if detected else 0,
        ))

    matches.sort(key=lambda m: m.similarity, reverse=True)
    return matches
