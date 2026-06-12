"""RORB simulation orchestrator.

Algorithm (topological routing):
  1. Process nodes from headwaters → outlet (Kahn's topological sort).
  2. Basin nodes: convert excess rainfall depth to flow [m³/s].
  3. Junction nodes: for each upstream reach, route the upstream node's
     hydrograph through that reach and sum at this junction.

This replicates the RORB control vector logic (codes 1/2/3/4/5) without
needing the explicit control vector — the topology encodes the same information.
"""

import numpy as np
from .attributes import Basin, Confluence
from .routing import route_rorb
from .rainfall import apply_il_cl, apply_proportional_loss


# ── avg_dist ──────────────────────────────────────────────────────────────────

def compute_avg_dist(catchment) -> float:
    """
    Area-weighted average travel distance from each sub-area to the outlet.

    RORB convention: k_i = kc × (length_i / avg_dist)
    This lets users enter the standard RORB kc value directly.
    Returns avg_dist in the same units as reach.length() (metres here).
    """
    sentinel = catchment._endSentinel
    nv = len(catchment._vertices)
    ne = len(catchment._edges)

    # Build downstream adjacency: node_idx -> [(downstream_idx, length)]
    downstream = {}
    for i in range(nv):
        for j in range(ne):
            dn = catchment._incidenceMatrixDS[i][j]
            if dn != sentinel:
                length = catchment._edges[j].length()
                downstream.setdefault(i, []).append((dn, length))

    outlet_idx = catchment._out

    def path_length(start):
        total = 0.0
        cur = start
        visited = set()
        while cur != outlet_idx:
            if cur in visited:
                break
            visited.add(cur)
            nexts = downstream.get(cur, [])
            if not nexts:
                break
            nxt, length = nexts[0]
            total += length
            cur = nxt
        return total

    total_area = 0.0
    weighted   = 0.0
    for idx, node in enumerate(catchment._vertices):
        if isinstance(node, Basin) and node.area > 0:
            d = path_length(idx)
            total_area += node.area
            weighted   += node.area * d

    return weighted / total_area if total_area > 0 else 1.0


# ── Topology helpers ──────────────────────────────────────────────────────────

def topological_order(catchment) -> list:
    """Return node indices from headwaters to outlet (Kahn's algorithm)."""
    nv = len(catchment._vertices)
    ne = len(catchment._edges)
    sentinel = catchment._endSentinel

    in_degree = np.zeros(nv, dtype=int)
    for dn in range(nv):
        for j in range(ne):
            if catchment._incidenceMatrixUS[dn][j] != sentinel:
                in_degree[dn] += 1

    order = []
    queue = [i for i in range(nv) if in_degree[i] == 0]

    while queue:
        node = queue.pop(0)
        order.append(node)
        for j in range(ne):
            dn = catchment._incidenceMatrixDS[node][j]
            if dn != sentinel:
                in_degree[dn] -= 1
                if in_degree[dn] == 0:
                    queue.append(dn)

    return order


# ── Main simulation ───────────────────────────────────────────────────────────

def run(catchment, kc: float, m: float, dt_hr: float,
        rainfall_mm: np.ndarray,
        loss_model: str = 'il_cl',
        il_mm: float = 25.0, cl_mm_hr: float = 2.5,
        prop_loss: float = 0.3,
        n_steps: int = None) -> dict:
    """
    Run RORB simulation and return results per node.

    Args:
        catchment   : connected Catchment object
        kc          : RORB routing coefficient  [hr * (m³/s)^(1-m) / km]
        m           : non-linearity exponent     (RORB default 0.8)
        dt_hr       : time step                  [hours]
        rainfall_mm : gross rainfall per step    [mm], length = storm steps
        loss_model  : 'il_cl' | 'proportional'
        il_mm       : initial loss               [mm]     (il_cl only)
        cl_mm_hr    : continuing loss rate       [mm/hr]  (il_cl only)
        prop_loss   : proportional loss fraction [0–1]    (proportional only)
        n_steps     : total steps (default: 4× storm steps for recession)

    Returns:
        dict keyed by node index:
            name        : node label
            node_type   : 'Basin' | 'Junction'
            is_outlet   : bool
            area_km2    : sub-area [km²] (Basin only)
            peak_flow   : peak discharge [m³/s]
            time_to_peak: time of peak  [hours from t=0]
            hydro       : np.array of flow [m³/s]
            time        : np.array of time [hours]
    """
    sentinel = catchment._endSentinel
    n_rain = len(rainfall_mm)
    if n_steps is None:
        n_steps = max(n_rain * 4, 50)

    # Pad rainfall to full simulation length
    rain_full = np.zeros(n_steps)
    rain_full[:min(n_rain, n_steps)] = rainfall_mm[:min(n_rain, n_steps)]

    # Compute excess rainfall (pervious fraction only — impervious handled below)
    if loss_model == 'il_cl':
        excess_pervious = apply_il_cl(rain_full, il_mm, cl_mm_hr, dt_hr)
    else:
        excess_pervious = apply_proportional_loss(rain_full, prop_loss)

    time_axis = np.arange(n_steps) * dt_hr

    # RORB convention: k_i = kc × (length_i / avg_dist)
    # avg_dist in metres (same units as reach.length())
    avg_dist_m = compute_avg_dist(catchment)

    order = topological_order(catchment)
    node_hydros = {}

    for node_idx in order:
        node = catchment._vertices[node_idx]

        if isinstance(node, Basin):
            fi = node.fi
            # Impervious areas: no infiltration loss (100% runoff)
            # Pervious areas: losses applied
            excess_total = excess_pervious * (1.0 - fi) + rain_full * fi
            # Convert depth [mm] + area [km²] + dt [hr] → flow [m³/s]
            q = excess_total * node.area / (dt_hr * 3.6)
            node_hydros[node_idx] = q

        else:  # Confluence / Junction
            combined = np.zeros(n_steps)
            for j in range(len(catchment._edges)):
                up_idx = catchment._incidenceMatrixUS[node_idx][j]
                if up_idx == sentinel:
                    continue
                reach = catchment._edges[j]
                up_hydro = node_hydros.get(up_idx, np.zeros(n_steps))
                # k = kc × (length / avg_dist)  — RORB standard normalisation
                k_reach = kc * reach.length() / avg_dist_m
                routed = route_rorb(up_hydro, k_reach, m, dt_hr)
                combined += routed
            node_hydros[node_idx] = combined

    # Package results
    results = {}
    for idx, node in enumerate(catchment._vertices):
        hydro = node_hydros.get(idx, np.zeros(n_steps))
        peak = float(np.max(hydro))
        peak_t_idx = int(np.argmax(hydro))
        results[idx] = {
            'name': node.name,
            'node_type': 'Basin' if isinstance(node, Basin) else 'Junction',
            'is_outlet': isinstance(node, Confluence) and node.isOut,
            'area_km2': node.area if isinstance(node, Basin) else 0.0,
            'peak_flow': peak,
            'time_to_peak': float(peak_t_idx) * dt_hr,
            'hydro': hydro,
            'time': time_axis,
        }

    return results
