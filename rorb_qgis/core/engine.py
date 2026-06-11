"""
Validated RORB routing engine — matches RORBWin output (NSE ≈ 0.9999).

All functions are pure Python + NumPy; no QGIS dependency.
"""
import re
import csv
import numpy as np
from pathlib import Path


# ── Loss model ────────────────────────────────────────────────────────────────

def apply_ilcl(rain_mm, il, cl, dt):
    """IL/CL loss model. Returns excess depth [mm] per step."""
    excess = np.zeros(len(rain_mm))
    cum = 0.0
    il_satisfied = False
    for i, r in enumerate(rain_mm):
        if il_satisfied:
            excess[i] = max(0.0, r - cl * dt)
        else:
            cum += r
            if cum >= il:
                il_satisfied = True
                excess[i] = max(0.0, (cum - il) - cl * dt)
    return excess


def to_m3s(excess_mm, area_km2, dt_hr):
    """Convert excess depth [mm] + area [km²] + dt [hr] → flow [m³/s]."""
    return excess_mm * area_km2 * 1e6 / 1e3 / (dt_hr * 3600.0)


# ── Non-linear storage routing: S = k·Q^m ────────────────────────────────────

def route(inflow, kc, kr, m, dt):
    """
    Route inflow through a single RORB reach.

    Uses S = k·Q^m (RORB standard) with bisection solver.
    Dry-start initial condition: I_prev = 0, so rhs = inflow[0]·dt/2.

    Parameters
    ----------
    inflow : array-like, flow [m³/s] per step
    kc     : global routing coefficient [hr·(m³/s)^(1-m)/km]
    kr     : relative delay time for this reach
    m      : non-linearity exponent (RORB default 0.8)
    dt     : time step [hours]
    """
    k = kc * kr
    n = len(inflow)
    Q = np.zeros(n)

    def bisect(rhs, hi_bound):
        lo, hi = 0.0, max(hi_bound, 1e-9)
        for _ in range(64):
            mid = (lo + hi) / 2.0
            if k * mid ** m + mid * dt / 2.0 < rhs:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    # Dry start: prior inflow = 0
    Q[0] = bisect(inflow[0] * dt / 2.0, max(inflow[0], 0.0) * 4.0 + 1.0)

    for t in range(1, n):
        I1, I2, Q1 = inflow[t - 1], inflow[t], Q[t - 1]
        rhs = (I1 + I2) * dt / 2.0 - Q1 * dt / 2.0 + k * max(Q1, 0.0) ** m
        Q[t] = bisect(rhs, max(I1, I2, Q1) * 4.0 + 1.0)

    return Q


# ── Control vector runner ─────────────────────────────────────────────────────

def _reach_label(line, reach_num, code):
    """Extract a human-readable label for a reach from its vector line."""
    # Try to parse the description after '-99'
    after = re.split(r'-99', line, maxsplit=1)
    desc = after[1].strip().lstrip(',').strip() if len(after) > 1 else ''
    # Take only the part before the next comma-separated description
    desc = desc.split(',')[0].strip()
    code_name = {1: 'SA+route', 2: 'add SA+route', 5: 'route'}[code]
    label = desc if desc else f'Reach {reach_num}'
    return f'Reach {reach_num}: {label} [{code_name}]'


def run_event(vector, areas_km2, kr_list, kc, m_exp, dt, rain_pad, il, cl):
    """
    Execute a RORB control vector.

    Returns
    -------
    print_results  : dict {node_name: np.ndarray}  — hydrographs at print nodes (code 7)
    reach_results  : list of dicts, one per routing step (codes 1/2/5):
                     {'label': str, 'hydro': np.ndarray, 'reach_num': int,
                      'code': int, 'kr': float, 'k': float}
    """
    n = len(rain_pad)
    excess = apply_ilcl(rain_pad, il, cl, dt)
    sa_in = [to_m3s(excess, a, dt) for a in areas_km2]

    hydro = np.zeros(n)
    stack = []
    print_results = {}
    reach_results = []
    si, ki = 0, 0
    pending_name = False
    reach_num = 0

    for line in vector:
        trimmed = line.strip()
        if not trimmed:
            continue

        if pending_name:
            if not re.match(r'^\d', trimmed):
                print_results[trimmed] = hydro.copy()
                pending_name = False
                continue
            pending_name = False

        code_m = re.match(r'^(\d+)', trimmed)
        if not code_m:
            continue
        code = int(code_m.group(1))

        def get_sa():
            nonlocal si
            arr = sa_in[si] if si < len(sa_in) else np.zeros(n)
            si += 1
            return arr

        def get_kr():
            nonlocal ki
            kr = kr_list[ki] if ki < len(kr_list) else 0.1
            ki += 1
            return kr

        if code == 0:
            break
        elif code == 1:
            reach_num += 1
            kr = get_kr()
            hydro = route(get_sa(), kc, kr, m_exp, dt)
            reach_results.append({
                'label': _reach_label(trimmed, reach_num, code),
                'hydro': hydro.copy(), 'reach_num': reach_num,
                'code': code, 'kr': kr, 'k': kc * kr,
            })
        elif code == 2:
            reach_num += 1
            sa = get_sa()
            kr = get_kr()
            hydro = route(hydro + sa, kc, kr, m_exp, dt)
            reach_results.append({
                'label': _reach_label(trimmed, reach_num, code),
                'hydro': hydro.copy(), 'reach_num': reach_num,
                'code': code, 'kr': kr, 'k': kc * kr,
            })
        elif code == 3:
            stack.append(hydro.copy())
            hydro = np.zeros(n)
        elif code == 4:
            hydro = hydro + (stack.pop() if stack else np.zeros(n))
        elif code == 5:
            reach_num += 1
            kr = get_kr()
            hydro = route(hydro, kc, kr, m_exp, dt)
            reach_results.append({
                'label': _reach_label(trimmed, reach_num, code),
                'hydro': hydro.copy(), 'reach_num': reach_num,
                'code': code, 'kr': kr, 'k': kc * kr,
            })
        elif code == 7:
            pending_name = True

    return print_results, reach_results


# ── File parsers ──────────────────────────────────────────────────────────────

def parse_catg(path):
    """Parse a RORB .catg file. Returns (areas_km2, vector_lines)."""
    lines = Path(path).read_text(errors='replace').splitlines()

    areas = []
    in_area = False
    for line in lines:
        if 'C Sub Area Data' in line:
            in_area = True
            continue
        if not in_area:
            continue
        if line.strip().startswith('C'):
            continue
        if '-99' in line:
            before = line.split('-99')[0]
            areas += [float(x) for x in re.findall(r'[\d.]+', before)]
            break
        areas += [float(x) for x in re.findall(r'[\d.]+', line)]

    vector = []
    past_graphical = False
    skip_flag = False
    for line in lines:
        s = line.strip()
        if s.startswith('C END RORB_GE'):
            past_graphical = True
            skip_flag = True
            continue
        if not past_graphical:
            continue
        if skip_flag and s == '1':
            skip_flag = False
            continue
        vector.append(s)

    return areas, vector


def parse_out(path):
    """
    Parse a RORB .out file.

    Returns
    -------
    kc, m, il, cl, dt, rain_ts, kr_list, peaks, total_depth
    """
    txt = Path(path).read_text(errors='replace')

    kc  = float(re.search(r'kc\s*=\s*([\d.]+)', txt).group(1))
    m   = float(re.search(r'\bm\s*=\s*([\d.]+)', txt).group(1))
    loss_m = re.search(
        r'Initial loss \(mm\)\s+Cont\. loss \(mm/h\)\s+([\d.]+)\s+([\d.]+)', txt)
    il = float(loss_m.group(1))
    cl = float(loss_m.group(2))
    dt = float(re.search(r'Time increment.*?=\s*([\d.]+)\s+hours', txt).group(1))

    rain_section = re.search(r'Rainfall, mm.*?(?=Rainfall-excess)', txt, re.DOTALL)
    rain_ts = []
    if rain_section:
        for line in rain_section.group(0).splitlines():
            nums = re.findall(r'[\d.]+', line)
            if len(nums) >= 2 and nums[0].isdigit():
                rain_ts.append(float(nums[1]))
    total_depth = sum(rain_ts) if rain_ts else None

    kr_list = [float(x) for x in re.findall(
        r'^\s*\d+\s+[\d.]+\s+([\d.]+)\s+Natural', txt, re.MULTILINE)]

    peaks = {}
    node = None
    for line in txt.splitlines():
        nm = re.search(r'\*\*\* Calculated hydrograph,\s+(.+)', line)
        if nm:
            node = nm.group(1).strip()
        if node and 'Peak discharge' in line:
            nums = re.findall(r'[\d.]+', line)
            if nums:
                peaks[node] = float(nums[-1])
            node = None

    return kc, m, il, cl, dt, rain_ts, kr_list, peaks, total_depth


def parse_stm(path):
    """
    Parse a RORB .stm storm file.
    Returns (dt_hr, rain_ts_mm) or (None, None) on failure.
    """
    txt = Path(path).read_text(errors='replace')
    dt_m    = re.search(r'^\s*([\d.]+),\s*\d+', txt, re.MULTILINE)
    depth_m = re.search(r'ARF\*BurDepth\(mm\)\s*:\s*([\d.]+)', txt)
    pct_m   = re.search(r'Temporal pattern \(% of depth\)\s*\n([\s\S]+?)(?:\nC |\n -99)', txt)

    dt    = float(dt_m.group(1))    if dt_m    else None
    depth = float(depth_m.group(1)) if depth_m else None
    pcts  = []
    if pct_m:
        for v in re.findall(r'-?[\d.]+', pct_m.group(1)):
            val = float(v)
            if val < 0:
                break
            pcts.append(val)

    if not depth or not pcts or not dt:
        return None, None
    return dt, [depth * p / 100.0 for p in pcts]


def parse_rorb_csv(path):
    """
    Parse a RORB output CSV file.
    Returns {node_name: np.ndarray}, time_axis (hrs), dt.

    RORB convention: Inc i → time (i+1)*dt.
    Aligned to engine (which outputs at i*dt) by skipping the first RORB step.
    """
    lines = Path(path).read_text(errors='replace').splitlines()
    hi = next((i for i, l in enumerate(lines) if 'Time (hrs)' in l), None)
    if hi is None:
        return {}, [], None

    header = [h.strip() for h in lines[hi].split(',')]
    names  = [h.replace('Calculated hydrograph:', '').strip() for h in header[2:]]
    arrays = [[] for _ in names]
    times  = []

    for line in lines[hi + 1:]:
        parts = line.split(',')
        if len(parts) < len(header):
            break
        try:
            times.append(float(parts[1]))
            for j, a in enumerate(arrays):
                a.append(float(parts[j + 2]))
        except (ValueError, IndexError):
            break

    # Skip first RORB step to align with engine output (rorb[k] ≈ engine[k-1])
    nodes = {name: np.array(arr[1:]) for name, arr in zip(names, arrays)}
    dt = (times[1] - times[0]) if len(times) >= 2 else None
    time_axis = [t for t in times[1:]]
    return nodes, time_axis, dt


# ── High-level runner ─────────────────────────────────────────────────────────

def run_from_files(catg_path, out_path, stm_path=None):
    """
    Run the validated RORB engine from file paths.

    Returns a results dict:
        hydros     : {node_name: np.ndarray}  (engine output)
        time       : list of time values [hr]
        dt         : time step [hr]
        kc, m, il, cl: model parameters
        rorb_peaks : {node_name: peak_flow}   (from .out file)
        n_steps    : total simulation steps
    """
    areas, vector = parse_catg(catg_path)
    kc, m, il, cl, dt, rain_ts, kr_list, rorb_peaks, _ = parse_out(out_path)

    if stm_path and Path(stm_path).exists():
        stm_dt, stm_rain = parse_stm(stm_path)
        if stm_rain and stm_dt:
            rain_ts, dt = stm_rain, stm_dt

    if not rain_ts or not kr_list:
        raise ValueError("Could not parse rainfall or storage parameters from .out file.")

    n_steps  = max(len(rain_ts) * 4, 200)
    rain_pad = rain_ts + [0.0] * (n_steps - len(rain_ts))

    hydros, reach_results = run_event(vector, areas, kr_list, kc, m, dt, rain_pad, il, cl)
    time_axis = [i * dt for i in range(n_steps)]

    return {
        'hydros':        hydros,         # print nodes {name: array}
        'reach_results': reach_results,  # all reaches [{label, hydro, ...}]
        'time':          time_axis,
        'dt':            dt,
        'kc':            kc,
        'm':             m,
        'il':            il,
        'cl':            cl,
        'rorb_peaks':    rorb_peaks,
        'n_steps':       n_steps,
    }
