"""
Storm setup utilities: parse IFD, temporal patterns and ARR Data Hub text.
No QGIS dependency — pure Python + stdlib.
"""
import re
import csv
from pathlib import Path


# ── AEP → temporal pattern band ───────────────────────────────────────────────

# Maps IFD column labels to ARR temporal pattern bands
_AEP_BAND = {
    '12EY': 'frequent', '6EY': 'frequent', '4EY': 'frequent',
    '3EY': 'frequent',  '2EY': 'frequent', '63.2%': 'frequent',
    '50%': 'frequent',  '0.5EY': 'frequent',
    '20%': 'intermediate', '10%': 'intermediate', '5%': 'intermediate',
    '2%': 'rare', '1%': 'rare', '0.2EY': 'rare',
    '1 in 200': 'rare', '1 in 500': 'rare',
    '1 in 1000': 'rare', '1 in 2000': 'rare',
}


def aep_to_band(aep_label):
    """Return 'frequent', 'intermediate', or 'rare' for an AEP label."""
    return _AEP_BAND.get(aep_label.strip(), 'intermediate')


def duration_label_to_minutes(label):
    """Parse '10 min' → 10, '1 hour' → 60, '1.5 hour' → 90, '1 day' → 1440."""
    m = re.match(r'([\d.]+)\s*(min|hour|hr|day)', label.strip(), re.IGNORECASE)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    if 'min' in unit:
        return round(val)
    if 'hour' in unit or 'hr' in unit:
        return round(val * 60)
    if 'day' in unit:
        return round(val * 1440)
    return None


# ── IFD CSV ────────────────────────────────────────────────────────────────────

def parse_ifd_csv(path):
    """
    Parse a BOM 'All Design Rainfall Depth (mm)' CSV.

    Returns
    -------
    aeps  : list of AEP label strings  (e.g. ['63.2%', '50%', …])
    rows  : list of dicts
              {'label': str, 'minutes': int, 'depths': {aep: float}}
    meta  : {'location': str, 'lat': float | None, 'lon': float | None}
    """
    lines = Path(path).read_text(errors='replace').splitlines()
    hi = next((i for i, ln in enumerate(lines) if 'Duration in min' in ln), None)
    if hi is None:
        raise ValueError("IFD CSV: cannot find 'Duration in min' header row.")

    header = [h.strip() for h in lines[hi].split(',')]
    aeps = header[2:]   # skip 'Duration' and 'Duration in min'

    rows = []
    for ln in lines[hi + 1:]:
        parts = [p.strip() for p in ln.split(',')]
        if len(parts) < 3 or not parts[0]:
            continue
        depths = {}
        for j, aep in enumerate(aeps):
            try:
                depths[aep] = float(parts[j + 2])
            except (ValueError, IndexError):
                pass
        if not depths:
            continue
        try:
            mins = int(parts[1])
        except ValueError:
            mins = duration_label_to_minutes(parts[0])
        rows.append({'label': parts[0], 'minutes': mins, 'depths': depths})

    # Metadata from header section
    lat_ln  = next((ln for ln in lines if 'Requested coordinate' in ln), '')
    loc_ln  = next((ln for ln in lines if 'Location Label' in ln), '')
    lat_m   = re.search(r'Latitude,([-\d.]+)', lat_ln)
    lon_m   = re.search(r'Longitude,([-\d.]+)', lat_ln)
    meta = {
        'location': loc_ln.split(',')[1].strip() if ',' in loc_ln else '',
        'lat': float(lat_m.group(1)) if lat_m else None,
        'lon': float(lon_m.group(1)) if lon_m else None,
    }
    return aeps, rows, meta


def get_ifd_depth(rows, aep, duration_minutes):
    """Look up burst depth [mm] for an AEP label and duration [minutes]."""
    row = next((r for r in rows if r['minutes'] == duration_minutes), None)
    return (row['depths'].get(aep) if row else None)


# ── Temporal pattern CSV ───────────────────────────────────────────────────────

def parse_temporal_patterns(path):
    """
    Parse an ARR 2016 temporal pattern CSV (e.g. ECnorth_Increments.csv).

    Returns a list of dicts:
        event_id, duration_min, timestep_min, region, aep_band,
        increments (list of % per step, sum ≈ 100)
    """
    patterns = []
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        next(reader, None)   # skip header
        for row in reader:
            if not row or not row[0].strip().isdigit():
                continue
            pcts = []
            for v in row[5:]:
                v = v.strip()
                if not v:
                    break
                try:
                    pcts.append(float(v))
                except ValueError:
                    break
            # drop trailing zeros
            while pcts and pcts[-1] == 0.0:
                pcts.pop()
            if not pcts:
                continue
            patterns.append({
                'event_id':     int(row[0]),
                'duration_min': int(row[1]),
                'timestep_min': int(row[2]),
                'region':       row[3].strip(),
                'aep_band':     row[4].strip().lower(),
                'increments':   pcts,
            })
    return patterns


def get_temporal_pattern(patterns, duration_min, aep_band, tp_num):
    """
    Retrieve temporal pattern for a given duration, AEP band and TP number (1-based).

    Returns (increments_pct, timestep_min) or (None, None) if not found.
    """
    cands = [p for p in patterns
             if p['duration_min'] == duration_min
             and p['aep_band'] == aep_band.lower()]
    if not cands or not (1 <= tp_num <= len(cands)):
        return None, None
    pat = cands[tp_num - 1]
    return pat['increments'], pat['timestep_min']


def available_tp_count(patterns, duration_min, aep_band):
    """Number of temporal patterns available for a duration + band."""
    return sum(1 for p in patterns
               if p['duration_min'] == duration_min
               and p['aep_band'] == aep_band.lower())


# ── ARR Data Hub TXT ───────────────────────────────────────────────────────────

def parse_arr_txt(path):
    """
    Parse an ARR Data Hub .txt export.

    Returns dict with:
        il       : initial loss [mm]
        cl       : continuing loss [mm/hr]
        longarf  : {'zone': str, 'a': float, … 'i': float}  (may be empty)
        preburst : raw block text
    """
    text = Path(path).read_text(errors='replace')

    def block(tag):
        m = re.search(rf'\[{tag}\]([\s\S]*?)\[END_{tag}\]', text)
        return m.group(1).strip() if m else ''

    result = {'il': None, 'cl': None, 'longarf': {}, 'preburst_raw': ''}

    losses = block('LOSSES')
    if losses:
        il_m = re.search(r'Storm Initial Losses \(mm\),([\d.]+)', losses)
        cl_m = re.search(r'Storm Continuing Losses \(mm/h\),([\d.]+)', losses)
        if il_m:
            result['il'] = float(il_m.group(1))
        if cl_m:
            result['cl'] = float(cl_m.group(1))

    arf_blk = block('LONGARF')
    if arf_blk:
        zone_m = re.search(r'Zone,(.+)', arf_blk)
        result['longarf']['zone'] = zone_m.group(1).strip() if zone_m else ''
        for param in 'abcdefghi':
            m = re.search(rf'(?:^|,){param},([-\d.]+)', arf_blk, re.MULTILINE)
            if m:
                result['longarf'][param] = float(m.group(1))

    result['preburst_raw'] = block('PREBURST')
    return result


# ── Rainfall builder ───────────────────────────────────────────────────────────

def build_rainfall_series(burst_depth_mm, arf, increments_pct):
    """
    Build per-step rainfall depths [mm].

    Parameters
    ----------
    burst_depth_mm : IFD burst depth [mm]
    arf            : areal reduction factor (0–1)
    increments_pct : temporal pattern percentages (sum ≈ 100)

    Returns
    -------
    list of mm per time step
    """
    catchment_depth = burst_depth_mm * arf
    return [catchment_depth * p / 100.0 for p in increments_pct]
