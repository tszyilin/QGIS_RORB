# -*- coding: utf-8 -*-
"""Pure-Python ARR2016 data loading and RORB .stm generation (no QGIS dependency)."""

import math
import re
import time
from collections import defaultdict

# AEP label → temporal pattern class in R_Increments.csv
# 0.2EY ≈ 18% AEP (5-yr ARI): treated as intermediate per ARR2016
AEP_CLASS = {
    '12EY':      'frequent',
    '6EY':       'frequent',
    '4EY':       'frequent',
    '3EY':       'frequent',
    '2EY':       'frequent',
    '63.2%':     'frequent',
    '50%':       'frequent',
    '0.5EY':     'frequent',
    '20%':       'frequent',      # 20% > 14.4% AEP → frequent
    '0.2EY':     'frequent',      # ≈18% AEP > 14.4% → frequent
    '10%':       'intermediate',  # 10% between 3.2% and 14.4% → intermediate
    '5%':        'intermediate',  # 5% between 3.2% and 14.4% → intermediate
    '2%':        'rare',
    '1%':        'rare',
    '1 in 200':  'very rare',
    '1 in 500':  'very rare',
    '1 in 1000': 'very rare',
    '1 in 2000': 'very rare',
}


def _normalize_aep_label(aep):
    """Normalise AEP label so CSV variants match the lookup tables.

    '63.20%' → '63.2%', '50.00%' → '50%', '1 in 10000' unchanged.
    '1% AEP' → '1%'  (ARR Data Hub IFD CSV header format)
    """
    aep = aep.strip()
    # Strip trailing " AEP" suffix produced by ARR Data Hub depth CSVs
    aep = re.sub(r'\s*AEP\s*$', '', aep, flags=re.IGNORECASE).strip()
    if aep.endswith('%'):
        try:
            return f'{float(aep[:-1]):g}%'
        except ValueError:
            pass
    return aep


def aep_to_class(aep_label):
    """Return temporal pattern class ('frequent'/'intermediate'/'rare') or None."""
    key = _normalize_aep_label(aep_label)
    result = AEP_CLASS.get(key)
    if result is not None:
        return result
    # Any '1 in X' with X > 2000 (extreme/PMP events) → very rare temporal patterns
    if key.startswith('1 in '):
        try:
            if int(key[5:].replace(',', '').split()[0]) > 2000:
                return 'very rare'
        except (ValueError, IndexError):
            pass
    return None


# Per-AEP tp number offset (matches RORBWin convention):
#   frequent (>14.4%)                     → tp1–10   (offset  0)
#   intermediate (3.2–14.4%: 10%, 5%)    → tp11–20  (offset 10)
#   rare (2%, 1%)                         → tp21–30  (offset 20)
#   very rare (1 in 200 and rarer)        → tp31–40  (offset 30)
_AEP_TP_OFFSET = {
    '12EY': 0, '6EY': 0, '4EY': 0, '3EY': 0, '2EY': 0,
    '63.2%': 0, '50%': 0, '0.5EY': 0,
    '20%': 0, '0.2EY': 0,
    '10%': 10, '5%': 10,
    '2%': 20, '1%': 20,
    '1 in 200': 30, '1 in 500': 30, '1 in 1000': 30, '1 in 2000': 30,
}


def tp_number(aep_label, position_1based):
    """Return sequential tp number for a given AEP and pattern position.

    Offsets match RORBWin:  intermediate → tp1-10, 10%/5% → tp11-20,
    2%/1% → tp21-30, 1-in-200 and rarer → tp31-40.
    """
    key = _normalize_aep_label(aep_label)
    offset = _AEP_TP_OFFSET.get(key)
    if offset is None:
        # Extreme '1 in X' events use the rare offset (30)
        if key.startswith('1 in '):
            try:
                if int(key[5:].replace(',', '').split()[0]) > 2000:
                    offset = 30
            except (ValueError, IndexError):
                pass
        if offset is None:
            offset = 0
    return offset + position_1based


def dur_label(dur_min):
    """Filename-safe duration label: '10min', '1hour', '1_5hour' (decimal → underscore)."""
    if dur_min < 60:
        return f'{dur_min}min'
    h = dur_min / 60.0
    if h == int(h):
        return f'{int(h)}hour'
    return f'{h:.1f}hour'.replace('.', '_')


def aep_filename_label(aep):
    """Filename-safe AEP label: '1%' → '1', '63.2%' → '63_2', '1 in 200' → '1in200'."""
    s = aep.replace('%', '').replace(' ', '').replace('/', '')
    return s.replace('.', '_')


def load_depths(csv_path):
    """
    Return ({dur_min: {aep_label: depth_mm}}, [aep_labels]).
    Header is at line index 9; data rows from line 10.
    """
    with open(csv_path, encoding='utf-8') as f:
        lines = f.readlines()
    header = [c.strip() for c in lines[9].split(',')]
    aep_cols_raw = header[2:]
    # Normalise headers: "1% AEP" → "1%", "50.00%" → "50%" etc.
    aep_cols = [_normalize_aep_label(a) for a in aep_cols_raw]
    aep_idx = {a_norm: i + 2 for i, a_norm in enumerate(aep_cols)}
    depths = {}
    for line in lines[10:]:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 3 or not parts[1].strip().isdigit():
            continue
        dur_min = int(parts[1])
        row = {}
        for aep, col in aep_idx.items():
            if col < len(parts) and parts[col]:
                try:
                    row[aep] = float(parts[col])
                except ValueError:
                    pass
        if row:
            depths[dur_min] = row
    return depths, aep_cols


def load_patterns(csv_path):
    """
    Return ({(dur_min, key): [(event_id, ts_min, [frac,...]), ...]}, areal_areas).

    Two formats from the ARR2016 Data Hub:
      Standard  R_Increments.csv : EventID, Duration, TimeStep, Region, AEP,  Inc1, ...
      Areal     R_Increments.csv : EventID, Duration, TimeStep, Region, Area, Inc1, ...

    Areal format: col-4 is numeric (standard area in km²).  The file may contain
    patterns for multiple standard areas (e.g. 100, 500, 1000, 5000 km²).  Patterns
    are keyed by (dur_min, area_float) so the caller can select the appropriate
    standard area for the catchment via select_standard_area().

    Returns:
      patterns    - dict keyed by (dur_min, aep_str) for standard, or
                    (dur_min, area_float) for areal format.
      areal_areas - sorted list of available standard area values, or None for
                    standard format.
    """
    patterns = defaultdict(list)
    areal_areas_set = set()
    is_areal = None

    with open(csv_path, encoding='utf-8') as f:
        lines = f.readlines()

    for line in lines[1:]:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 6:
            continue
        try:
            event_id = int(parts[0])
            dur_min  = int(parts[1])
            ts_min   = int(parts[2])
        except ValueError:
            continue

        col4 = parts[4]
        try:
            area = float(col4)
            is_areal = True
            areal_areas_set.add(area)
            fracs = [float(p) for p in parts[5:] if p.strip()]
            if fracs:
                patterns[(dur_min, area)].append((event_id, ts_min, fracs))
        except ValueError:
            is_areal = False
            fracs = [float(p) for p in parts[5:] if p.strip()]
            if fracs:
                patterns[(dur_min, col4)].append((event_id, ts_min, fracs))

    areal_areas = sorted(areal_areas_set) if is_areal else None
    return patterns, areal_areas


def select_standard_area(available_areas, catchment_area_km2):
    """Pick the best standard area for a given catchment area.

    Returns the smallest standard area >= catchment_area_km2,
    or the largest available if none is large enough.
    """
    if not available_areas:
        return None
    for a in sorted(available_areas):
        if a >= catchment_area_km2:
            return a
    return max(available_areas)


def load_arf_params(hub_path):
    """Return ARF long-duration coefficient dict {a..i: float} from ARR Data Hub .txt, or None."""
    params = {}
    in_section = False
    with open(hub_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if s == '[LONGARF]':
                in_section = True
                continue
            if in_section:
                if s.startswith('[END'):
                    break
                if ',' in s:
                    key, val = s.split(',', 1)
                    key = key.strip().lower()
                    if len(key) == 1 and key in 'abcdefghi':
                        try:
                            params[key] = float(val.strip())
                        except ValueError:
                            pass
    return params if len(params) == 9 else None


# ARF_short fixed global parameters (ARR2019, all regions, dur ≤ 720 min)
_ARF_SHORT = (0.287, 0.265, 0.439, 0.36, 0.00226, 0.226, 0.125, 0.0141, -0.021, 0.213)

# AEP label → fraction for ARF calculation
_AEP_FRAC = {
    '12EY': 1.0,  '6EY': 0.9975, '4EY': 0.9817, '3EY': 0.9502, '2EY': 0.8647,
    '63.2%': 0.632, '50%': 0.50, '0.5EY': 0.3935,
    '20%': 0.20, '0.2EY': 0.1813,
    '10%': 0.10, '5%': 0.05, '2%': 0.02, '1%': 0.01,
    '1 in 200': 0.005, '1 in 500': 0.002, '1 in 1000': 0.001, '1 in 2000': 0.0005,
}


def aep_label_to_fraction(aep_label):
    """Return AEP as a fraction (e.g. '20%' → 0.20). Returns 0.10 if unknown."""
    key = _normalize_aep_label(aep_label)
    if key in _AEP_FRAC:
        return _AEP_FRAC[key]
    # Compute fraction from '1 in X' format
    if key.startswith('1 in '):
        try:
            return 1.0 / int(key[5:].replace(',', '').split()[0])
        except (ValueError, IndexError, ZeroDivisionError):
            pass
    return 0.10


def _arf_short(area_km2, dur_min, aep_frac):
    """ARR2019 short-duration ARF (dur ≤ 720 min) with fixed global parameters."""
    a, b, c, d, e, f, g, h, i, j = _ARF_SHORT
    lp = 0.3 + math.log10(aep_frac)
    t1 = a * (area_km2**b - c * math.log10(dur_min)) * dur_min**(-d)
    t2 = e * area_km2**f * dur_min**g * lp
    t3 = h * area_km2**j * 10**(i * (dur_min - 180)**2 / 1440) * lp
    return min(1.0, 1.0 - t1 + t2 + t3)


def _arf_long(arf_params, area_km2, dur_min, aep_frac):
    """ARR2019 long-duration ARF (dur ≥ 1440 min) with region-specific hub parameters."""
    a, b, c, d = arf_params['a'], arf_params['b'], arf_params['c'], arf_params['d']
    e, f, g, h, i = arf_params['e'], arf_params['f'], arf_params['g'], arf_params['h'], arf_params['i']
    lp = 0.3 + math.log10(aep_frac)
    t1 = a * (area_km2**b - c * math.log10(dur_min)) * dur_min**(-d)
    t2 = e * area_km2**f * dur_min**g * lp
    t3 = h * 10**(i * area_km2 * dur_min / 1440) * lp
    return min(1.0, 1.0 - t1 + t2 + t3)


def _arf_at_area(fn10, area_km2):
    """Interpolate ARF for 1 < area < 10 km² from the ARF at 10 km²."""
    return 1.0 - 0.6614 * (1.0 - fn10) * (area_km2**0.4 - 1.0)


def calc_arf(arf_params, dur_min, area_km2, aep_frac=0.10):
    """ARR2019 ARF.  Returns ARF in [0, 1].

    aep_frac: AEP as a fraction (e.g. 0.20 for 20%).  Use aep_label_to_fraction().
    Short-duration ARF (≤ 12 h) uses fixed global params — no hub file needed.
    Long-duration ARF (≥ 24 h) requires arf_params from load_arf_params(); returns 1.0 if None.

    Both short- and long-duration ARF use the actual design AEP, matching RORBWin behaviour.
    """
    if area_km2 <= 1.0:
        return 1.0
    aep_frac = max(0.0005, min(0.5, aep_frac))

    if dur_min <= 720:
        if area_km2 >= 10:
            return max(0.0, _arf_short(area_km2, dur_min, aep_frac))
        return _arf_at_area(_arf_short(10.0, dur_min, aep_frac), area_km2)

    if dur_min >= 1440:
        if arf_params is None:
            return 1.0
        if area_km2 >= 10:
            return _arf_long(arf_params, area_km2, dur_min, aep_frac)
        return _arf_at_area(_arf_long(arf_params, 10.0, dur_min, aep_frac), area_km2)

    # 720 < dur < 1440: interpolate between ARF_short(720) and ARF_long(1440)
    if arf_params is None:
        return 1.0
    if area_km2 >= 10:
        s12 = _arf_short(area_km2, 720, aep_frac)
        l24 = _arf_long(arf_params, area_km2, 1440, aep_frac)
        return s12 + (l24 - s12) * (dur_min - 720) / 720
    s12 = _arf_short(10.0, 720, aep_frac)
    l24 = _arf_long(arf_params, 10.0, 1440, aep_frac)
    interp_10 = s12 + (l24 - s12) * (dur_min - 720) / 720
    return _arf_at_area(interp_10, area_km2)


def write_stm(out_path, fracs, ts_min, raw_depth_mm, arf,
              catg_path='', catg_name='', aep='', dur_display_str='', tp_num=1,
              area_km2=0.0, n_subareas=1, calc_incs=200,
              is_areal=False, standard_area_km2=None):
    """Write a RORB DESIGN storm file in RORBWin comment-header format.

    fracs: percentage increments (list summing to ~100).
    raw_depth_mm: IFD burst depth before ARF.
    arf: areal reduction factor (written depth = raw_depth_mm * arf).
    """
    depth_mm = raw_depth_mm * arf
    ts_h = ts_min / 60.0
    burst_incs = len(fracs)
    n_subareas = max(1, n_subareas)

    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(f'{catg_name}: {dur_display_str} {aep} Design Storm No.{tp_num} Temporal Pattern\n')
        fh.write('DESIGN\n')
        fh.write('C  \n')
        fh.write(f'C  Date run           : {time.strftime("%d %b %Y %H:%M")}\n')
        fh.write(f'C  Catchment file     : {catg_path}\n')
        fh.write(f'C  Storm area (km²)   :      {area_km2:.2f}\n')
        fh.write(f'C  Storm ARI (yr)     : {aep}\n')
        fh.write(f'C  Storm duration     : {dur_display_str}\n')
        if is_areal and standard_area_km2 is not None:
            tp_desc = f'ARR2016 pattern {tp_num} (areal temporal patterns, standard area = {standard_area_km2:.0f} km\xb2)'
        else:
            tp_desc = f'ARR2016 pattern {tp_num} (point temporal patterns)'
        fh.write(f'C  Temporal pattern   : {tp_desc}\n')
        fh.write('C  Spatial pattern    : Uniform\n')
        fh.write(f'C  Burst depth (mm)   :   {raw_depth_mm:.2f}\n')
        fh.write(f'C  Areal Red. Fact.   :     {arf:.2f} (ARR2016 approach)\n')
        fh.write(f'C  ARF*BurDepth(mm)   :   {depth_mm:.2f}\n')
        fh.write('C  \n')
        fh.write('C  \n')
        fh.write(f'   {ts_h:.5f},{calc_incs}, 1,  1, 1, -99\n')
        fh.write(f' 0,  {burst_incs}\n')
        fh.write('Temporal pattern (% of depth)\n')
        pct = [f'{v:6.2f}' for v in fracs]
        for ci in range(0, len(pct), 13):
            chunk = pct[ci:ci + 13]
            is_last = (ci + 13 >= len(pct))
            fh.write(','.join(chunk) + (',-99.00\n' if is_last else ',\n'))
        fh.write(f'C  Sub-area rainfall depths (areally weighted average depth =     {depth_mm:.2f} mm)\n')
        sub = [f' {depth_mm:.2f}' for _ in range(n_subareas)]
        for si in range(0, len(sub), 10):
            chunk = sub[si:si + 10]
            is_last = (si + 10 >= len(sub))
            fh.write(','.join(chunk) + (', -99.00\n' if is_last else ',\n'))


def dur_display(dur_min):
    """Human-readable duration with space separator: '10 min', '72 hour'."""
    if dur_min < 60:
        return f'{dur_min} min'
    return f'{dur_min // 60} hour'


def parse_all_peaks(out_path):
    """Return [(node_name, peak_m3s), ...] in print-node order from a RORB .out file."""
    nodes = []
    current_name = None
    try:
        with open(out_path, encoding='latin-1') as f:
            for line in f:
                if '*** Calculated hydrograph,' in line:
                    current_name = line.split('*** Calculated hydrograph,', 1)[1].strip()
                elif '*** Calc. hyd. for ungauged interstation site at:' in line:
                    current_name = line.split('*** Calc. hyd. for ungauged interstation site at:', 1)[1].strip()
                elif 'Peak discharge' in line and current_name is not None:
                    for tok in reversed(line.split()):
                        try:
                            nodes.append((current_name, float(tok)))
                            break
                        except ValueError:
                            pass
                    current_name = None
    except OSError:
        pass
    return nodes


def parse_node_legend(out_path):
    """Return [(num_str, node_name), ...] from the legend at the end of a RORB .out file."""
    legend = []
    try:
        with open(out_path, encoding='latin-1') as f:
            for line in f:
                # lines like "   01  Calculated hydrograph,  Western_02"
                m = line.strip()
                if m and m[:2].strip().isdigit() and 'Calculated hydrograph' in m:
                    parts = m.split(None, 1)
                    if len(parts) == 2:
                        desc = parts[1].strip()
                        name = desc.split(',', 1)[-1].strip() if ',' in desc else desc
                        legend.append((parts[0].zfill(2), name))
    except OSError:
        pass
    return legend


def parse_peak_flow(out_path):
    """Return maximum peak discharge (m3/s) from a RORB .out file, or None."""
    peaks = parse_all_peaks(out_path)
    if not peaks:
        return None
    return max(v for _, v in peaks)
