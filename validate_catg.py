"""
validate_catg.py  —  Validate our routing engine against a RORB .catg file.

Parses any RORB GE .catg file (v002 or v6.x) and runs our engine with the
same network, then optionally compares to RORB_CMD.exe output.

Usage
-----
  python validate_catg.py  <catchment.catg>  [options]

Options
  --kc      RORB routing coefficient      (default 1.5)
  --m       Non-linearity exponent         (default 0.8)
  --rain    Total rainfall depth mm        (default 80)
  --dur     Storm duration hours           (default 6)
  --dt      Time step hours                (default 0.5)
  --il      Initial loss mm                (default 20)
  --cl      Continuing loss mm/hr          (default 2.0)
  --rorb    Path to RORB_CMD.exe (auto-detected if omitted)

Example
  python validate_catg.py RORBWin/SampleData/Fig6_7.catg --kc 1.5
"""

import argparse, os, re, subprocess, sys, tempfile
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from rorb_qgis.core.attributes import Basin, Confluence, Reach, ReachType
from rorb_qgis.core.catchment  import Catchment
from rorb_qgis.core.rainfall   import uniform_pattern, apply_il_cl
from rorb_qgis.core.simulation import run, topological_order


# ─────────────────────────────────────────────────────────────────────────────
# .catg parser — strategy:
#   Graphics block → node positions, type (basin/junction), outlet flag,
#                    each node's ds_node_id, reach length_km
#   Vector block   → sub-area areas km² and fi fractions (explicit data tables)
#   Topology       → reconstructed from node ds_node_id pointers (avoids
#                    ambiguous reach-line parsing for us/ds fields)
# ─────────────────────────────────────────────────────────────────────────────

def _numeric_tokens(line):
    """Return list of floats from all numeric tokens in a line."""
    result = []
    for tok in line.split():
        try:
            result.append(float(tok))
        except ValueError:
            pass
    return result


def parse_catg(filepath):
    """
    Returns
    -------
    nodes   : {node_id: {x, y, is_basin, is_outlet, ds_node, name}}
    reaches : {reach_id: {us, ds, length_km}}  — us/ds corrected via ds_node pointers
    areas   : [area_km2, ...] in vector-block traversal order
    fis     : [fi, ...]        in vector-block traversal order
    """
    with open(filepath, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()

    nodes   = {}
    reaches = {}
    in_nodes = in_reaches = False

    for line in lines:
        s = line.strip()

        if '#NODES'   in s: in_nodes = True;  in_reaches = False; continue
        if '#REACHES' in s: in_nodes = False; in_reaches = True;  continue
        if any(k in s for k in ('#STORAGES','#INFLOW','END RORB_GE')):
            in_nodes = in_reaches = False

        # ── node line ──────────────────────────────────────────────────────
        if in_nodes and s.startswith('C'):
            body = s[1:].strip()
            if not body or not body[0].isdigit():
                continue
            nums = _numeric_tokens(body)
            if len(nums) < 7:
                continue
            try:
                nid       = int(nums[0])
                x, y      = nums[1], nums[2]
                is_basin  = int(nums[4]) == 1
                is_outlet = int(nums[5]) == 1
                ds_node   = int(nums[6])
                tokens    = body.split()
                name_tok  = next((t for t in tokens[4:] if not _is_num(t)), str(nid))
                nodes[nid] = dict(x=x, y=y, is_basin=is_basin,
                                  is_outlet=is_outlet, ds_node=ds_node,
                                  name=name_tok, area=0.0, fi=0.0)
            except (ValueError, IndexError):
                pass

        # ── reach line ─────────────────────────────────────────────────────
        if in_reaches and s.startswith('C'):
            body = s[1:].strip()
            if not body or not body[0].isdigit():
                continue
            nums = _numeric_tokens(body)
            if len(nums) < 6:
                continue
            try:
                rid    = int(nums[0])
                length = nums[-4]   # length is 4th from end in both old and 6.x formats
                # Extract candidate node IDs (integers in plausible node range)
                # We'll validate direction using ds_node pointers after all nodes parsed
                cands = _ordered_unique(
                    int(v) for v in nums[1:]
                    if v == int(v) and 1 <= int(v) <= 500
                )
                us = cands[0] if len(cands) > 0 else 0
                ds = cands[1] if len(cands) > 1 else 0
                reaches[rid] = dict(us=us, ds=ds, length_km=length)
            except (ValueError, IndexError):
                pass

    # ── Correct reach directions using node ds_node pointers ─────────────
    for rid, rd in reaches.items():
        us, ds = rd['us'], rd['ds']
        if us in nodes and ds in nodes:
            if nodes[us]['ds_node'] == ds:
                pass   # already correct
            elif nodes[ds]['ds_node'] == us:
                rd['us'], rd['ds'] = ds, us   # swap
            # If neither check passes, leave as-is (best guess)

    # ── Vector block: sub-area areas and fi ──────────────────────────────
    areas = _parse_data_table(lines, 'Areas, km')
    fis   = _parse_fi_table(lines)

    return nodes, reaches, areas, fis


def _ordered_unique(iterable):
    seen, result = set(), []
    for v in iterable:
        if v not in seen:
            seen.add(v); result.append(v)
    return result


def _is_num(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _parse_data_table(lines, marker):
    """Parse a comma/space-separated numeric table following a line containing marker."""
    found, values = False, []
    for line in lines:
        if marker in line:
            found = True
            continue
        if found:
            if line.strip().startswith('C') or line.strip() == '':
                if values:
                    break
                continue
            for tok in re.split(r'[,\s]+', line.strip()):
                if tok == '-99':
                    return values
                try:
                    values.append(float(tok))
                except ValueError:
                    pass
    return values


def _parse_fi_table(lines):
    """Parse the impervious fraction table from the vector block."""
    found, values = False, []
    for line in lines:
        if 'Impervious Fraction' in line:
            found = True
            continue
        if found and not line.strip().startswith('C'):
            # first data line: "1, fi1, fi2, ..., -99"  or "0, -99"
            parts = re.split(r'[,\s]+', line.strip())
            code = parts[0].strip() if parts else ''
            if code == '0':
                return []          # code 0 = no impervious areas
            if code == '1':
                for tok in parts[1:]:
                    if tok == '-99':
                        return values
                    try:
                        values.append(float(tok))
                    except ValueError:
                        pass
                found = False
    return values


# ─────────────────────────────────────────────────────────────────────────────
# Build Catchment from parsed data
# ─────────────────────────────────────────────────────────────────────────────

def build_catchment(nodes, reaches, areas, fis):
    """
    Topology strategy
    -----------------
    Each node stores its ds_node_id (the node it drains to).
    We create one Reach per node→ds_node pair, taking the reach length
    from reach_lengths matched in order (or estimated from coordinate distance).

    Areas/fi are assigned to basin nodes in topological order (headwaters→outlet),
    matching the order they appear in the vector-block data tables.
    """
    if not nodes:
        raise ValueError("No nodes parsed from .catg file.")

    # Sort node IDs
    sorted_ids = sorted(nodes.keys())

    # Separate into confluences + basins
    conf_ids  = [nid for nid in sorted_ids if not nodes[nid]['is_basin']]
    basin_ids = [nid for nid in sorted_ids if     nodes[nid]['is_basin']]

    # Build objects
    conf_objs  = [Confluence(nodes[nid]['name'],
                             nodes[nid]['x'], nodes[nid]['y'],
                             nodes[nid]['is_outlet'])
                  for nid in conf_ids]
    basin_objs = [Basin(nodes[nid]['name'],
                        nodes[nid]['x'], nodes[nid]['y'],
                        0.0, 0.0)
                  for nid in basin_ids]

    # Map node_id → vertex index in Catchment._vertices
    id_to_idx = {}
    for k, nid in enumerate(conf_ids):
        id_to_idx[nid] = k
    for k, nid in enumerate(basin_ids):
        id_to_idx[nid] = len(conf_ids) + k

    # Build Reach objects directly from the parsed reach data (us, ds, length_km)
    reach_objs = []
    for rid, rd in sorted(reaches.items()):
        us_nid = rd['us']
        ds_nid = rd['ds']
        if us_nid not in nodes or ds_nid not in nodes:
            continue
        us_nd = nodes[us_nid]
        ds_nd = nodes[ds_nid]
        coords = [(us_nd['x'], us_nd['y']), (ds_nd['x'], ds_nd['y'])]
        r = Reach(f"R{rid}", coords, ReachType.NATURAL, 0.0)
        r._length = rd['length_km'] * 1000   # internal storage is metres
        reach_objs.append(r)

    cat = Catchment(conf_objs, basin_objs, reach_objs)
    cat.connect()

    # ── Assign areas and fi to basin nodes in topological order ───────────
    order = topological_order(cat)
    basin_order = [i for i in order
                   if isinstance(cat._vertices[i], Basin)]

    for k, vi in enumerate(basin_order):
        if k < len(areas):
            cat._vertices[vi]._area = areas[k]
        if k < len(fis):
            cat._vertices[vi]._fi = fis[k]

    return cat


# ─────────────────────────────────────────────────────────────────────────────
# Optional RORB_CMD runner
# ─────────────────────────────────────────────────────────────────────────────

_CMD_PATHS = [
    r"C:\Program Files\RORB\RORB_CMD.exe",
    r"C:\Program Files (x86)\RORB\RORB_CMD.exe",
    os.path.join(os.path.dirname(__file__), "RORBWin", "RORB_CMD.exe"),
]

def find_cmd(hint=None):
    if hint and os.path.isfile(hint):
        return hint
    return next((p for p in _CMD_PATHS if os.path.isfile(p)), None)


def make_design_stm(total_mm, dur_hr, dt_hr, n_sub, path):
    n = max(1, round(dur_hr / dt_hr))
    d = total_mm / n
    row = ','.join(f'{d:.3f}' for _ in range(n))
    pct = ','.join('100' for _ in range(n_sub))
    ref = ','.join('1'   for _ in range(n_sub))
    with open(path, 'w') as f:
        f.write(f"Design {total_mm:.0f}mm/{dur_hr:.0f}hr uniform\n")
        f.write("DESIGN\n")
        f.write(f"{dt_hr:.2f},{n},1,1,0,-99\n")
        f.write(f"0,{n}\n")
        f.write("Pluviograph\n")
        f.write(f"{row},-99\n")
        f.write(f"{pct},-99\n")
        f.write(f"{ref},-99\n")


def run_rorb_cmd(catg, stm, kc, m, exe):
    """Run RORB_CMD.exe, return stdout text."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.par',
                                     delete=False, dir=tempfile.gettempdir()) as pf:
        pf.write("# BEGIN\n")
        pf.write(f"Cat file :{catg}\n")
        pf.write(f"Stm file :{stm}\n")
        pf.write("Lumped kc:T\n")
        pf.write("Verbosity:3\n")
        pf.write("Lossmodel:1\n")
        pf.write(f"Kc       :{kc:.4f}\n")
        pf.write(f"M        :{m:.4f}\n")
        pf.write("# END\n")
        par = pf.name
    try:
        r = subprocess.run([exe, par], capture_output=True,
                           text=True, timeout=60)
        return r.stdout + r.stderr
    except Exception as e:
        return f"[RORB_CMD failed: {e}]"
    finally:
        try: os.unlink(par)
        except: pass


def parse_cmd_peaks(text):
    peaks = {}
    for line in text.splitlines():
        # Pattern: "Peak flow at <name> = <value> m^3/s" (RORB_CMD verbose output)
        m = re.search(r'Peak\s+flow[^=]*=\s*([\d.]+)\s*m', line, re.I)
        nm = re.search(r'at\s+(\S+)', line, re.I)
        if m and nm:
            peaks[nm.group(1)] = float(m.group(1))
        # Alt pattern
        m2 = re.search(r'(\w+)\s*peak\s*=\s*([\d.]+)', line, re.I)
        if m2:
            peaks[m2.group(1)] = float(m2.group(2))
    return peaks


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('catg')
    ap.add_argument('--kc',   type=float, default=1.5)
    ap.add_argument('--m',    type=float, default=0.8)
    ap.add_argument('--rain', type=float, default=80.0,  dest='total_mm')
    ap.add_argument('--dur',  type=float, default=6.0,   dest='dur')
    ap.add_argument('--dt',   type=float, default=0.5)
    ap.add_argument('--il',   type=float, default=20.0)
    ap.add_argument('--cl',   type=float, default=2.0)
    ap.add_argument('--rorb', default=None,  dest='rorb_cmd')
    args = ap.parse_args()

    print(f"\nParsing: {args.catg}")
    nodes, reaches, areas, fis = parse_catg(args.catg)

    n_basins = sum(1 for n in nodes.values() if n['is_basin'])
    n_junctions = len(nodes) - n_basins
    print(f"  Nodes:   {len(nodes)}  ({n_basins} basins, {n_junctions} junctions)")
    print(f"  Reaches: {len(reaches)}")
    print(f"  Areas from vector block:  {areas}")
    print(f"  Fi    from vector block:  {fis}")

    if not areas:
        print("WARNING: No areas found in vector block. Check file format.")

    print(f"\nBuilding catchment…")
    cat = build_catchment(nodes, reaches, areas, fis)
    total_area = sum(v._area for v in cat._vertices if isinstance(v, Basin))
    print(f"  Total area: {total_area:.3f} km2  "
          f"({sum(1 for v in cat._vertices if isinstance(v, Basin))} basins)")

    # Rainfall
    n_steps = max(1, round(args.dur / args.dt))
    rain = uniform_pattern(args.total_mm, n_steps)
    print(f"\nRainfall: {args.total_mm:.0f} mm / {args.dur:.1f} hr, "
          f"IL={args.il} mm, CL={args.cl} mm/hr")
    print(f"Routing:  kc={args.kc}, m={args.m}, dt={args.dt} hr")

    # Run our engine
    print("\nRunning engine…")
    results = run(cat, kc=args.kc, m=args.m, dt_hr=args.dt,
                  rainfall_mm=rain, loss_model='il_cl',
                  il_mm=args.il, cl_mm_hr=args.cl,
                  n_steps=n_steps * 5)

    our = {r['name']: r['peak_flow']
           for r in results.values() if r['node_type'] == 'Junction'}

    # Optionally run RORB_CMD
    cmd_peaks = {}
    exe = find_cmd(args.rorb_cmd)
    if exe:
        print(f"Running RORB_CMD: {exe}")
        stm_tmp = tempfile.mktemp(suffix='.stm')
        make_design_stm(args.total_mm, args.dur, args.dt, n_basins, stm_tmp)
        raw = run_rorb_cmd(os.path.abspath(args.catg), stm_tmp,
                           args.kc, args.m, exe)
        cmd_peaks = parse_cmd_peaks(raw)
        try: os.unlink(stm_tmp)
        except: pass
        if not cmd_peaks:
            print("  RORB_CMD ran but peak flows could not be parsed from output.")
            print("  Tip: compare to your RORB results manually using the table below.")
    else:
        print("RORB_CMD.exe not found — showing our results only.")
        print("  Compare the 'Our engine' column to your RORB output manually.")

    # Print table
    all_names = sorted(set(list(our.keys()) + list(cmd_peaks.keys())))
    print()
    print("=" * 72)
    print(f"{'Node':<14} {'Area km2':>9}  {'Our engine':>12}  {'RORB_CMD':>12}  {'Diff %':>8}")
    print("-" * 72)
    for name in all_names:
        o = our.get(name, 0.0)
        c = cmd_peaks.get(name, None)
        area_str = ""
        res = next((r for r in results.values() if r['name'] == name), None)
        is_out = res['is_outlet'] if res else False
        tag = " <- OUTLET" if is_out else ""
        if c is not None:
            diff = (o - c) / c * 100 if c > 0 else float('nan')
            print(f"{name:<14} {area_str:>9}  {o:>12.3f}  {c:>12.3f}  {diff:>+7.2f}%{tag}")
        else:
            print(f"{name:<14} {area_str:>9}  {o:>12.3f}  {'(run RORB)':>12}{tag}")
    print("=" * 72)

    outlet = next((r for r in results.values() if r['is_outlet']), None)
    if outlet:
        excess = apply_il_cl(
            np.concatenate([rain, np.zeros(len(rain)*4)]),
            args.il, args.cl, args.dt)
        vol_in  = excess.sum() / 1000 * total_area * 1e6 / 1e6
        vol_out = outlet['hydro'].sum() * args.dt * 3600 / 1e6
        print(f"\nOutlet: {outlet['name']}  "
              f"peak = {outlet['peak_flow']:.3f} m3/s  "
              f"at t = {outlet['time_to_peak']:.2f} hr")
        print(f"Volume: in = {vol_in:.3f} Mm3   out = {vol_out:.3f} Mm3   "
              f"balance = {vol_out/vol_in*100:.1f}%" if vol_in > 0 else "")


if __name__ == '__main__':
    main()
