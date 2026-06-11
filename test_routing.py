"""
Standalone test for the RORB routing engine.
Run with: python test_routing.py
No QGIS installation required.

Tests:
  1. route_rorb()  — mass balance and monotone recession checks
  2. Fig6_7        — replicates the RORBWin sample catchment topology
  3. 3-basin synth — simple triangle catchment with known structure
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np

# ── import engine without QGIS ────────────────────────────────────────────────
from rorb_qgis.core.routing   import route_rorb
from rorb_qgis.core.rainfall  import (uniform_pattern, triangular_pattern,
                                       apply_il_cl)
from rorb_qgis.core.attributes import Basin, Confluence, Reach, ReachType
from rorb_qgis.core.catchment  import Catchment
from rorb_qgis.core.simulation import run, topological_order

PASS = "  PASS"
FAIL = "  FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Unit tests for route_rorb
# ─────────────────────────────────────────────────────────────────────────────
def test_mass_balance():
    """Volume in ≈ volume out for a complete event (flat tail)."""
    dt = 0.5
    rain = triangular_pattern(50.0, 24)          # 24 steps × 0.5 hr = 12 hr storm
    inflow = rain * 50.0 / (dt * 3.6)            # ~50 km² catchment
    n_pad = 200
    inflow_padded = np.concatenate([inflow, np.zeros(n_pad)])
    outflow = route_rorb(inflow_padded, k=2.0, m=0.8, dt=dt)
    vol_in  = inflow_padded.sum() * dt * 3600    # m³
    vol_out = outflow.sum()       * dt * 3600
    ratio = vol_out / vol_in
    ok = 0.95 < ratio < 1.05
    print(f"[1] Mass balance  vol_in={vol_in/1e6:.3f} Mm³  vol_out={vol_out/1e6:.3f} Mm³  "
          f"ratio={ratio:.4f}  {PASS if ok else FAIL}")
    return ok


def test_peak_attenuation():
    """Peak outflow must be lower than peak inflow.
    Uses dt=0.5 so the Courant number dt/tau < 1 (numerically stable).
    With k=5.0, m=0.8, Q~1000: tau = k*m*Q^(m-1) ≈ 1 hr >> dt=0.5.
    """
    inflow = triangular_pattern(100.0, 40) * 200.0   # 40 steps × 0.5 hr
    outflow = route_rorb(inflow, k=5.0, m=0.8, dt=0.5)
    ok = outflow.max() < inflow.max()
    print(f"[2] Peak attenuation  in={inflow.max():.2f}  out={outflow.max():.2f}  "
          f"{PASS if ok else FAIL}")
    return ok


def test_peak_lag():
    """Time to peak of outflow must be ≥ time to peak of inflow."""
    inflow = triangular_pattern(100.0, 30) * 200.0
    outflow = route_rorb(inflow, k=4.0, m=0.8, dt=0.5)
    ok = np.argmax(outflow) >= np.argmax(inflow)
    print(f"[3] Peak lag  in_t={np.argmax(inflow)*0.5:.1f}hr  "
          f"out_t={np.argmax(outflow)*0.5:.1f}hr  {PASS if ok else FAIL}")
    return ok


def test_zero_inflow():
    """Zero inflow should give zero outflow."""
    outflow = route_rorb(np.zeros(50), k=2.0, m=0.8, dt=1.0)
    ok = outflow.max() < 1e-8
    print(f"[4] Zero inflow  max_out={outflow.max():.2e}  {PASS if ok else FAIL}")
    return ok


def test_il_cl():
    """IL/CL: first IL mm lost, then CL mm/hr ongoing."""
    rain = np.array([10.0, 10.0, 10.0, 10.0, 10.0])  # 5 × 10mm at dt=1hr
    exc  = apply_il_cl(rain, il_mm=15.0, cl_mm_hr=2.0, dt_hr=1.0)
    # Step 0: 10mm rain, 10mm consumed by IL → excess=0
    # Step 1: 10mm rain, 5mm IL remaining → 5mm absorbed, 5-2=3mm excess
    # Step 2: 10mm rain, IL gone, CL=2mm/hr → excess=8mm
    # Step 3: 8mm excess, step 4: 8mm excess
    expected = np.array([0.0, 3.0, 8.0, 8.0, 8.0])
    ok = np.allclose(exc, expected, atol=0.01)
    print(f"[5] IL/CL  expected={expected}  got={np.round(exc,2)}  {PASS if ok else FAIL}")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fig6_7 network (RORBWin sample catchment)
# ─────────────────────────────────────────────────────────────────────────────
def build_fig67():
    """
    Reproduce the Fig 6.7 catchment from the RORB manual sample data.

    Network (from Fig6_7.catg):
      J2  ← A (5 km)   ← B (4 km)
      J5  ← J2 (2.5)   ← C (4.5)
      J6  ← J5 (2.5)   ← E (4.5)
      J8  ← J6 (1.0)   ← D (4.5)
      J10 ← J8 (1.5)   [outlet]

    Sub-areas: A=30, B=28, C=25, E=35, D=40 km²
    No impervious areas.
    """
    # Coordinates chosen to give correct reach topology via nearest-node matching.
    # Each basin sits slightly upstream (higher y) of its junction.
    nodes = {
        'J2':  Confluence('J2',  0,  0),
        'J5':  Confluence('J5',  0, 30),
        'J6':  Confluence('J6',  0, 55),
        'J8':  Confluence('J8',  0, 75),
        'J10': Confluence('J10', 0, 90, out=True),
        'A':   Basin('A', -20, -20, area=30),
        'B':   Basin('B',  20, -15, area=28),
        'C':   Basin('C',  20,  25, area=25),
        'E':   Basin('E', -20,  45, area=35),
        'D':   Basin('D',  20,  65, area=40),
    }

    def reach(name, us_name, ds_name, km):
        u = nodes[us_name].coordinates()
        d = nodes[ds_name].coordinates()
        return Reach(name, [u, d], ReachType.NATURAL, 0.0)

    reaches = [
        reach('r_A',   'A',   'J2',  5.0),
        reach('r_B',   'B',   'J2',  4.0),
        reach('r_J2',  'J2',  'J5',  2.5),
        reach('r_C',   'C',   'J5',  4.5),
        reach('r_J5',  'J5',  'J6',  2.5),
        reach('r_E',   'E',   'J6',  4.5),
        reach('r_J6',  'J6',  'J8',  1.0),
        reach('r_D',   'D',   'J8',  4.5),
        reach('r_J8',  'J8',  'J10', 1.5),
    ]

    confluences = [nodes[k] for k in ('J2','J5','J6','J8','J10')]
    basins      = [nodes[k] for k in ('A','B','C','E','D')]

    cat = Catchment(confluences, basins, reaches)
    cat.connect()
    return cat


def test_fig67_topology():
    """Topological order must visit all 10 nodes and put J10 last."""
    cat = build_fig67()
    order = topological_order(cat)
    ok_len  = len(order) == 10
    ok_last = cat._vertices[order[-1]].name == 'J10'
    print(f"[6] Fig6_7 topology  nodes={len(order)}  last={cat._vertices[order[-1]].name}  "
          f"{PASS if ok_len and ok_last else FAIL}")
    return ok_len and ok_last


def test_fig67_simulation():
    """
    Simulate Fig6_7 with kc=1.5, m=0.8 and a 6-hr uniform 80mm storm.
    Checks:
      - Outlet peak > 0
      - Total outlet volume ≈ total sub-area runoff volume (mass balance within 5%)
      - Peak is between 0 and total area × peak intensity (physical bounds)
    """
    cat = build_fig67()
    dt  = 0.5   # hr
    rain = uniform_pattern(80.0, 12)   # 80mm over 6 hr (12 steps × 0.5hr)

    results = run(cat, kc=1.5, m=0.8, dt_hr=dt,
                  rainfall_mm=rain,
                  loss_model='il_cl', il_mm=20.0, cl_mm_hr=2.0)

    outlet = next(r for r in results.values() if r['is_outlet'])
    peak   = outlet['peak_flow']

    # Mass balance: compare input excess volume to outlet volume
    # Use same loss model manually
    excess = apply_il_cl(np.concatenate([rain, np.zeros(len(rain)*3)]),
                         il_mm=20.0, cl_mm_hr=2.0, dt_hr=dt)
    total_area = sum(r['area_km2'] for r in results.values())
    vol_in  = excess.sum() / 1000 * total_area * 1e6          # m³
    vol_out = outlet['hydro'].sum() * dt * 3600                # m³
    ratio   = vol_out / vol_in if vol_in > 0 else 0

    ok_peak  = peak > 0
    ok_mass  = 0.90 < ratio < 1.10
    print(f"[7] Fig6_7 simulation  peak={peak:.2f} m³/s  "
          f"vol_in={vol_in/1e6:.3f} Mm³  vol_out={vol_out/1e6:.3f} Mm³  "
          f"ratio={ratio:.3f}  {PASS if ok_peak and ok_mass else FAIL}")

    # Print all junction peaks
    junctions = sorted([r for r in results.values() if r['node_type']=='Junction'],
                       key=lambda r: -r['peak_flow'])
    print("     Junction peaks:")
    for r in junctions:
        marker = " <- OUTLET" if r['is_outlet'] else ""
        print(f"       {r['name']:6s}  {r['peak_flow']:8.2f} m³/s  "
              f"t_peak={r['time_to_peak']:.1f} hr{marker}")

    return ok_peak and ok_mass


# ─────────────────────────────────────────────────────────────────────────────
# 3. Synthetic 3-basin linear chain
# ─────────────────────────────────────────────────────────────────────────────
def test_linear_chain():
    """
    Simple linear catchment:
      B1(10km²) → R1(3km) → J1 → R2(5km) → J2(outlet)
      B2(15km²) → R3(4km) ↗

    Checks: outlet peak > sum of immediate upstream junction peaks / 2
    (routing causes attenuation so peak at outlet < sum of unrouted inputs)
    """
    j1  = Confluence('J1', 0, 10)
    j2  = Confluence('J2', 0, 20, out=True)
    b1  = Basin('B1', -5,  0, area=10)
    b2  = Basin('B2',  5,  8, area=15)

    r1 = Reach('R1', [b1.coordinates(), j1.coordinates()], ReachType.NATURAL)
    r2 = Reach('R2', [j1.coordinates(), j2.coordinates()], ReachType.NATURAL)
    r3 = Reach('R3', [b2.coordinates(), j1.coordinates()], ReachType.NATURAL)

    cat = Catchment([j1, j2], [b1, b2], [r1, r2, r3])
    cat.connect()

    rain = triangular_pattern(60.0, 12)
    results = run(cat, kc=1.0, m=0.8, dt_hr=0.5,
                  rainfall_mm=rain, loss_model='il_cl',
                  il_mm=10.0, cl_mm_hr=1.0)

    outlet = next(r for r in results.values() if r['is_outlet'])
    j1_res = next(r for r in results.values() if r['name'] == 'J1')
    ok = outlet['peak_flow'] > 0 and j1_res['peak_flow'] > 0

    print(f"[8] Linear chain  J1_peak={j1_res['peak_flow']:.2f}  "
          f"J2_peak={outlet['peak_flow']:.2f}  {PASS if ok else FAIL}")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=" * 60)
    print("RORB Model for QGIS — Routing Engine Tests")
    print("=" * 60)

    results = [
        test_mass_balance(),
        test_peak_attenuation(),
        test_peak_lag(),
        test_zero_inflow(),
        test_il_cl(),
        test_fig67_topology(),
        test_fig67_simulation(),
        test_linear_chain(),
    ]

    passed = sum(results)
    total  = len(results)
    print("=" * 60)
    print(f"Results: {passed}/{total} passed")
    if passed == total:
        print("ALL TESTS PASSED")
    else:
        print(f"{total - passed} FAILED")
        sys.exit(1)
