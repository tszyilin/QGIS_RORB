# -*- coding: utf-8 -*-
"""Pure-Python ARR2016 data loading and RORB .stm generation (no QGIS dependency)."""

import math
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
    '1 in 200':  'rare',
    '1 in 500':  'rare',
    '1 in 1000': 'rare',
    '1 in 2000': 'rare',
}


def aep_to_class(aep_label):
    """Return temporal pattern class ('frequent'/'intermediate'/'rare') or None."""
    return AEP_CLASS.get(aep_label)


# Per-AEP tp number offset (matches RORBWin convention):
#   frequent (>14.4%) / intermediate (3.2–14.4%) → tp1–10   (offset  0)
#   intermediate 10%/5%                           → tp11–20  (offset 10)
#   rare 2%/1%                                    → tp21–30  (offset 20)
#   very rare 1-in-200 and beyond                 → tp31–40  (offset 30)
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
    return _AEP_TP_OFFSET.get(aep_label, 0) + position_1based


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
    aep_cols = header[2:]
    aep_idx = {a: i + 2 for i, a in enumerate(aep_cols)}
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
    Return {(dur_min, aep_class): [(ts_min, [frac,...]), ...]} from R_Increments.csv.
    Columns: EventID, Duration, TimeStep, Region, AEP, Inc1, Inc2, ...
    """
    patterns = defaultdict(list)
    with open(csv_path, encoding='utf-8') as f:
        lines = f.readlines()
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 6:
            continue
        try:
            event_id = int(parts[0])
            dur_min = int(parts[1])
            ts_min = int(parts[2])
        except ValueError:
            continue
        aep_cls = parts[4]
        fracs = [float(p) for p in parts[5:] if p.strip()]
        if fracs:
            patterns[(dur_min, aep_cls)].append((event_id, ts_min, fracs))
    return patterns


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
    return _AEP_FRAC.get(aep_label, 0.10)


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

    RORBWin uses the actual AEP for ARF_short but fixes aep=0.10 for ARF_long calls
    (including the long-duration anchor used in the 12–24 h interpolation).
    """
    if area_km2 <= 1.0:
        return 1.0
    aep_frac = max(0.0005, min(0.5, aep_frac))
    _LONG_AEP = 0.10  # RORBWin fixes ARF_long at 10% AEP regardless of design AEP

    if dur_min <= 720:
        if area_km2 >= 10:
            return max(0.0, _arf_short(area_km2, dur_min, aep_frac))
        return _arf_at_area(_arf_short(10.0, dur_min, aep_frac), area_km2)

    if dur_min >= 1440:
        if arf_params is None:
            return 1.0
        if area_km2 >= 10:
            return _arf_long(arf_params, area_km2, dur_min, _LONG_AEP)
        return _arf_at_area(_arf_long(arf_params, 10.0, dur_min, _LONG_AEP), area_km2)

    # 720 < dur < 1440: interpolate between ARF_short(720, actual AEP) and ARF_long(1440, 10%)
    if arf_params is None:
        return 1.0
    if area_km2 >= 10:
        s12 = _arf_short(area_km2, 720, aep_frac)
        l24 = _arf_long(arf_params, area_km2, 1440, _LONG_AEP)
        return s12 + (l24 - s12) * (dur_min - 720) / 720
    s12 = _arf_short(10.0, 720, aep_frac)
    l24 = _arf_long(arf_params, 10.0, 1440, _LONG_AEP)
    interp_10 = s12 + (l24 - s12) * (dur_min - 720) / 720
    return _arf_at_area(interp_10, area_km2)


def write_stm(out_path, fracs, ts_min, raw_depth_mm, arf,
              catg_path='', catg_name='', aep='', dur_display_str='', tp_num=1,
              area_km2=0.0, n_subareas=1, calc_incs=200):
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
        fh.write(f'C  Temporal pattern   : ARR2016 pattern, {tp_num} (point temporal patterns)\n')
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
