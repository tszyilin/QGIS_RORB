# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2026-06-27'
__copyright__ = '(C) 2026 by Tom Norman'

import ctypes
import csv as _csv_mod
import os
import re
import shutil
import subprocess
import tempfile
import json
import time

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QTextEdit, QFileDialog, QMessageBox,
    QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QDoubleSpinBox,
    QHeaderView, QApplication, QTabWidget, QRadioButton, QButtonGroup,
    QScrollArea, QWidget,
)
from qgis.PyQt.QtCore import Qt, QThread, QSettings
from qgis.PyQt.QtGui import QFont

try:
    from qgis.PyQt.QtCore import pyqtSignal
except ImportError:
    from qgis.PyQt.QtCore import Signal as pyqtSignal

from .compat import ALIGN_RIGHT, ALIGN_CENTER
from . import arr2016_runner as _arr


# ── Regional Kc equations ────────────────────────────────────────────────────
# Each entry: (display_label, formula_type, coeff, exponent, area_limit_km2)
#   formula_type 'A'   → kc = coeff × A^exponent   (A in km², area_limit enforced)
#   formula_type 'Dav' → kc = coeff × Dav           (Dav in km, exponent/area_limit unused)
_REGIONAL_KC_EQNS = [
    # ── Area-based (ARR Book 7, 2019) ────────────────────────────────────────
    ('Eastern NSW (Kleemola) - Eqn 7.6.13, ARR(BkVII)', 'A', 1.18, 0.46, None),
    ('Vic (MAR>800mm) - Eqn 7.6.15, ARR(BkVII)',        'A', 2.57, 0.45, None),
    ('Vic (MAR<800mm) - Eqn 7.6.16, ARR(BkVII)',        'A', 0.49, 0.65, None),
    ('Queensland (Weeks) - Eqn 7.6.9, ARR(BkVII)',      'A', 0.88, 0.53, None),
    ('S.A. (A<100km²) - Eqn 7.6.17, ARR(BkVII)',        'A', 0.60, 0.67, 100.0),
    ('S. Tas (A<100km²) - Eqn 7.6.17, ARR(BkVII)',      'A', 0.60, 0.67, 100.0),
    ('West Tas (HEC) - Eqn 7.6.29, ARR(BkVII)',         'A', 0.86, 0.57, None),
    ('Nth Flinders Rngs S.A. (MAR<300mm) (Kemp,pers.comm)', 'A', 0.33, 0.52, None),
    # ── Dav-based (area-standardised lag, Pearse et al. 2002) ─────────────
    ('Victoria data (Pearse et al, 2002)',                'Dav', 1.25, None, None),
    ('Aus wide Dyer (1994) data (Pearse et al, 2002)',    'Dav', 1.14, None, None),
    ('Aus wide Yu (1989) data (Pearse et al, 2002)',      'Dav', 0.96, None, None),
    ('Pilbara W.A. (Pearcey et al, 2014)',                'Dav', 0.59, None, None),
]


# ── Suggest Kc dialog ────────────────────────────────────────────────────────

class SuggestKcDialog(QDialog):
    """Suggest kc values via RORB Manual default or ARR regional equations."""

    kc_adopted = pyqtSignal(int, float)  # (row_index, kc_value)

    def __init__(self, parent, isa_groups, areas_km2, av_dists_km, peak_m3s=None):
        """
        isa_groups   : list[str]   — row labels matching the Parameters table
        areas_km2    : list[float] — catchment area per group (km²)
        av_dists_km  : list[float|None] — average flow distance per group (km)
        peak_m3s     : float|None  — max peak flow from last run (display only)
        """
        super().__init__(parent)
        self.setWindowTitle('Suggest values for Kc')
        self.setMinimumWidth(400)
        self._isa_groups  = isa_groups
        self._areas_km2   = areas_km2
        self._av_dists_km = av_dists_km
        self._peak_m3s    = peak_m3s
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Catchment attributes ─────────────────────────────────────────────
        grp_attr = QGroupBox('Catchment attributes')
        attr_lay = QVBoxLayout(grp_attr)

        self._isa_combo = QComboBox()
        self._isa_combo.addItems(self._isa_groups)
        self._isa_combo.currentIndexChanged.connect(self._refresh)
        attr_lay.addWidget(self._isa_combo)

        attr_form = QFormLayout()
        attr_form.setLabelAlignment(ALIGN_RIGHT)
        self._lbl_area   = QLabel('—')
        self._lbl_avdist = QLabel('—')
        self._lbl_peak   = QLabel('—')
        attr_form.addRow('Catchment area =',       self._lbl_area)
        attr_form.addRow('Average flow distance =', self._lbl_avdist)
        attr_form.addRow('Maximum peak flow =',     self._lbl_peak)
        attr_lay.addLayout(attr_form)

        user_row = QHBoxLayout()
        self._chk_user_peak = QCheckBox('User-supplied max flow')
        self._chk_user_peak.stateChanged.connect(
            lambda s: self._spn_user_peak.setEnabled(bool(s)))
        user_row.addWidget(self._chk_user_peak)
        self._spn_user_peak = QDoubleSpinBox()
        self._spn_user_peak.setRange(0, 1e9)
        self._spn_user_peak.setDecimals(1)
        self._spn_user_peak.setValue(0.0)
        self._spn_user_peak.setFixedWidth(100)
        self._spn_user_peak.setEnabled(False)
        self._spn_user_peak.setSuffix(' m³/s')
        user_row.addWidget(self._spn_user_peak)
        user_row.addStretch()
        attr_lay.addLayout(user_row)

        note = QLabel('* or other function of reach properties related to travel time')
        note.setStyleSheet('color: grey; font-size: 8pt;')
        attr_lay.addWidget(note)
        root.addWidget(grp_attr)

        # ── Default (RORB Manual Eqn 2.5) ───────────────────────────────────
        grp_default = QGroupBox('Default  (Eqn 2.5  Rorb Manual)')
        def_lay = QFormLayout(grp_default)
        def_lay.setLabelAlignment(ALIGN_RIGHT)
        def_row = QHBoxLayout()
        self._lbl_kc_default = QLabel('—')
        def_row.addWidget(self._lbl_kc_default)
        def_row.addStretch()
        btn_adopt_def = QPushButton('Adopt')
        btn_adopt_def.setFixedWidth(70)
        btn_adopt_def.clicked.connect(self._adopt_default)
        def_row.addWidget(btn_adopt_def)
        def_lay.addRow('kc  =', def_row)
        root.addWidget(grp_default)

        # ── Regional ─────────────────────────────────────────────────────────
        grp_reg = QGroupBox('Regional')
        reg_lay = QVBoxLayout(grp_reg)

        self._reg_combo = QComboBox()
        for label, *_ in _REGIONAL_KC_EQNS:
            self._reg_combo.addItem(label)
        self._reg_combo.currentIndexChanged.connect(self._refresh_regional)
        reg_lay.addWidget(self._reg_combo)

        self._lbl_reg_formula = QLabel('')
        self._lbl_reg_formula.setStyleSheet('color: #555; font-style: italic;')
        reg_lay.addWidget(self._lbl_reg_formula)

        reg_form = QFormLayout()
        reg_form.setLabelAlignment(ALIGN_RIGHT)
        reg_row = QHBoxLayout()
        self._lbl_kc_reg = QLabel('—')
        reg_row.addWidget(self._lbl_kc_reg)
        reg_row.addStretch()
        btn_adopt_reg = QPushButton('Adopt')
        btn_adopt_reg.setFixedWidth(70)
        btn_adopt_reg.clicked.connect(self._adopt_regional)
        reg_row.addWidget(btn_adopt_reg)
        reg_form.addRow('kc  =', reg_row)
        reg_lay.addLayout(reg_form)
        root.addWidget(grp_reg)

        # ── Footer ────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_help = QPushButton('Help')
        btn_help.clicked.connect(self._on_help)
        btn_row.addWidget(btn_help)
        btn_row.addStretch()
        btn_close = QPushButton('Close')
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    def _current_area(self):
        idx = self._isa_combo.currentIndex()
        return self._areas_km2[idx] if 0 <= idx < len(self._areas_km2) else None

    def _current_avdist(self):
        idx = self._isa_combo.currentIndex()
        return self._av_dists_km[idx] if 0 <= idx < len(self._av_dists_km) else None

    def _refresh(self):
        area   = self._current_area()
        avdist = self._current_avdist()
        peak   = self._peak_m3s

        self._lbl_area.setText(f'{area:.1f} km²' if area is not None else '—')
        self._lbl_avdist.setText(f'{avdist:.2f} km*' if avdist is not None else '—')
        self._lbl_peak.setText(f'{peak:.1f} m³/s' if peak is not None else '—')

        if area and area > 0:
            self._lbl_kc_default.setText(f'{2.2 * (area ** 0.5):.2f}')
        else:
            self._lbl_kc_default.setText('—')

        self._refresh_regional()

    def _refresh_regional(self):
        idx = self._reg_combo.currentIndex()
        if not (0 <= idx < len(_REGIONAL_KC_EQNS)):
            return
        _, ftype, coeff, exp, alim = _REGIONAL_KC_EQNS[idx]

        if ftype == 'Dav':
            self._lbl_reg_formula.setText(f'kc = {coeff} * Dav')
            avdist = self._current_avdist()
            if avdist is not None and avdist > 0:
                self._lbl_kc_reg.setText(f'{coeff * avdist:.2f}')
            else:
                self._lbl_kc_reg.setText('— (no Dav)')
        else:
            self._lbl_reg_formula.setText(f'kc = {coeff} × A^{exp}')
            area = self._current_area()
            if area and area > 0:
                if alim is not None and area > alim:
                    self._lbl_kc_reg.setText(f'N/A (A > {alim:.0f} km²)')
                else:
                    self._lbl_kc_reg.setText(f'{coeff * (area ** exp):.2f}')
            else:
                self._lbl_kc_reg.setText('—')

    def _adopt_default(self):
        area = self._current_area()
        if area and area > 0:
            self.kc_adopted.emit(self._isa_combo.currentIndex(), 2.2 * (area ** 0.5))

    def _adopt_regional(self):
        idx = self._reg_combo.currentIndex()
        if not (0 <= idx < len(_REGIONAL_KC_EQNS)):
            return
        _, ftype, coeff, exp, alim = _REGIONAL_KC_EQNS[idx]

        if ftype == 'Dav':
            avdist = self._current_avdist()
            if avdist is not None and avdist > 0:
                self.kc_adopted.emit(self._isa_combo.currentIndex(), coeff * avdist)
        else:
            area = self._current_area()
            if area and area > 0 and (alim is None or area <= alim):
                self.kc_adopted.emit(self._isa_combo.currentIndex(), coeff * (area ** exp))

    def _on_help(self):
        QMessageBox.information(
            self, 'Kc Equations',
            'Default (Eqn 2.5 RORB Manual):\n'
            '  kc = 2.2 × √A\n\n'
            'Area-based regional equations:\n'
            '  kc = coeff × A^exponent\n'
            '  where A = catchment area (km²)\n\n'
            'Dav-based regional equations (area-standardised lag):\n'
            '  kc = coeff × Dav\n'
            '  where Dav = average flow distance (km)\n\n'
            'Source: ARR Book 7 (2019), Section 6.2.1–6.2.2')


# ── ASCII-safe path helpers ──────────────────────────────────────────────────

def _to_short_path(path):
    """Try Windows 8.3 short path; fall back to original if still non-ASCII."""
    if not path:
        return path
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    result = buf.value if n else path
    try:
        result.encode('ascii')
        return result
    except UnicodeEncodeError:
        return path


def _is_ascii(path):
    try:
        path.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


# ── RORB_CMD.exe discovery ──────────────────────────────────────────────────

_CMD_PATHS = [
    r'C:\Program Files\RORBWin\RORB_CMD.exe',
    r'C:\Program Files\RORB\RORB_CMD.exe',
    r'C:\Program Files (x86)\RORB\RORB_CMD.exe',
]


def find_rorb_cmd(hint=None):
    if hint and os.path.isfile(hint):
        return hint
    return next((p for p in _CMD_PATHS if os.path.isfile(p)), None)


# ── .catg parsing — ordered interstation-area list ──────────────────────────

def _numeric_tokens(line):
    out = []
    for tok in line.split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


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


def parse_catg_areas(path):
    """
    Return an ordered list of {'name', 'area_km2', 'print_code'} dicts — one
    entry per interstation (sub-)area in .catg #NODES traversal order.
    """
    with open(path, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()

    basin_names = []
    basin_print_codes = []
    in_nodes = False
    for line in lines:
        s = line.strip()
        if '#NODES' in s:
            in_nodes = True
            continue
        if any(k in s for k in ('#REACHES', '#STORAGES', '#INFLOW', 'END RORB_GE')):
            in_nodes = False
        if in_nodes and s.startswith('C'):
            body = s[1:].strip()
            if not body or not body[0].isdigit():
                continue
            nums = _numeric_tokens(body)
            if len(nums) < 7:
                continue
            is_basin = int(nums[4]) == 1
            if is_basin:
                tokens = body.split()
                name = next((t for t in tokens[4:] if not _is_num(t)),
                            f'Area{len(basin_names) + 1}')
                basin_names.append(name)
                print_code = int(nums[9]) if len(nums) > 9 else 0
                basin_print_codes.append(print_code)

    areas = _parse_data_table(lines, 'Areas, km')

    n = max(len(basin_names), len(areas))
    result = []
    for i in range(n):
        name = basin_names[i] if i < len(basin_names) else f'Area {i + 1}'
        area = areas[i] if i < len(areas) else 0.0
        pc = basin_print_codes[i] if i < len(basin_print_codes) else 0
        result.append({'name': name, 'area_km2': area, 'print_code': pc})
    return result


def parse_catg_isa_groups(path):
    """
    Return an ordered list of ISA group name strings for the Parameters table.

    Scans the #NODES block for print-code nodes:
      - print code 72 (7.2) = dummy-print ISA boundary; name on next C line
      - is_outlet flag = 1   = catchment outlet;        name on next C line

    Returns [isa_1, isa_2, ..., outlet_name].
    Falls back to ['ISA 1'] when no print nodes are found.
    """
    with open(path, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()

    outlet_name = None
    dummy_names = []

    in_nodes = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if '#NODES' in s:
            in_nodes = True
            continue
        if any(k in s for k in ('#REACHES', '#STORAGES', '#INFLOW', 'END RORB_GE')):
            break
        if not in_nodes or not s.startswith('C'):
            continue
        body = s[1:].strip()
        if not body or not body[0].isdigit():
            continue
        nums = _numeric_tokens(body)
        if len(nums) < 6:
            continue

        is_outlet   = int(nums[5]) == 1
        # Print code is the 10th numeric token; 72 = instruction 7.2 dummy-print
        is_print_72 = len(nums) >= 10 and int(nums[9]) == 72

        if not (is_outlet or is_print_72):
            continue

        # Name is on the immediately following C comment line
        name = None
        while i < len(lines):
            ns = lines[i].strip()
            i += 1
            if ns.startswith('C'):
                candidate = ns[1:].strip()
                if candidate:
                    name = candidate
                break

        if is_print_72:
            dummy_names.append(name or f'Node {len(dummy_names) + 1}')
        if is_outlet:
            outlet_name = name

    if dummy_names:
        return dummy_names + [outlet_name or 'outlet']
    return [outlet_name or 'ISA 1']


def parse_catg_isa_count(path):
    """Return the number of ISA parameter groups (see parse_catg_isa_groups)."""
    return len(parse_catg_isa_groups(path))


def parse_catg_calc_order(path):
    """
    Parse .catg nodes, reaches, and control vector.

    Returns (rows, missing) where:
      rows    – list of dicts: calc_no, code_str, node, reach, storage,
                io, name, io_id, av_dist_km
      missing – dict: nodes, reaches, storages, io  (lists of str)
    """
    with open(path, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()

    # ── #NODES ───────────────────────────────────────────────────────────────
    nodes = {}        # id → {is_basin, downstream, area, name}
    basin_order = []  # basin node IDs in appearance order
    in_sec = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if '#NODES' in s:
            in_sec = True
            continue
        if any(k in s for k in ('#REACHES', '#STORAGES', '#INFLOW', 'END RORB_GE')):
            in_sec = False
        if not in_sec or not s.startswith('C'):
            continue
        body = s[1:].strip()
        if not body or not body[0].isdigit():
            continue
        nums = _numeric_tokens(body)
        if len(nums) < 7:
            continue
        nid = int(nums[0])
        is_basin = int(nums[4]) == 1
        dn_raw = int(nums[6]) if len(nums) > 6 else 0
        dn = dn_raw if dn_raw != 0 else None
        # print_code: at nums[9] for non-basin nodes (70 = Code 7, 72 = Code 7.2)
        print_code = int(nums[9]) if len(nums) > 9 else 0
        # Name: peek at the immediately following C line for text-only content
        name = None
        if i < len(lines):
            ns = lines[i].strip()
            if ns.startswith('C'):
                cand = ns[1:].strip()
                if cand and not cand[0].isdigit():
                    name = cand
        nodes[nid] = {
            'is_basin': is_basin,
            'downstream': dn,
            'area': 0.0,
            'name': name or str(nid),
            'print_code': print_code,
            'has_text_name': name is not None,
        }
        if is_basin:
            basin_order.append(nid)

    # Areas are filled after parsing the control vector (Code-1 order), not here.

    # Queues of unnamed print nodes in #NODES appearance order, used as fallback
    # for Code-7 instructions that have no text name following them.
    from collections import deque
    _unnamed_q70 = deque(
        nid for nid, n in nodes.items()
        if n['print_code'] == 70 and not n['has_text_name']
    )
    _unnamed_q72 = deque(
        nid for nid, n in nodes.items()
        if n['print_code'] == 72 and not n['has_text_name']
    )

    # Detect RORB_GE format version — determines #REACHES field layout.
    # Old (< 4.0, e.g. "002"): seq  from  to  flags  length
    # New (>= 4.0):            seq  reach_id_or_name  from  to  flags  length
    #   reach_id_or_name is either an integer (== seq) or a text name.
    _ge_ver = 0.0
    for _l in lines[:20]:
        _m = re.search(r'C\s+RORB_GE\s+([\d.]+)', _l)
        if _m:
            try:
                _ge_ver = float(_m.group(1))
            except ValueError:
                pass
            break
    _new_reach_fmt = _ge_ver >= 4.0

    # ── #REACHES ─────────────────────────────────────────────────────────────
    reaches = {}  # id → {from_node, to_node, length_km}
    in_sec = False
    for line in lines:
        s = line.strip()
        if '#REACHES' in s:
            in_sec = True
            continue
        if any(k in s for k in ('#STORAGES', '#INFLOW', 'END RORB_GE')):
            in_sec = False
        if not in_sec or not s.startswith('C'):
            continue
        body = s[1:].strip()
        if not body or not body[0].isdigit():
            continue
        nums = _numeric_tokens(body)
        if len(nums) < 7:
            continue

        if _new_reach_fmt:
            # Check whether the second raw token is a number (numbered reach,
            # seq_id == reach_id) or text (named reach, skipped by _numeric_tokens).
            raw2 = body.split()[1] if len(body.split()) > 1 else ''
            try:
                _v = float(raw2)
                is_numbered = (_v == float(int(_v)) and int(_v) == int(nums[0]))
            except ValueError:
                is_numbered = False  # text reach name

            if is_numbered:
                # Numbered: seq reach_id(=seq) from to flags... length
                # Coordinate lines also start with floats — filter them out by
                # requiring nums[0] == nums[1] and both integral.
                if nums[0] != nums[1] or nums[0] != float(int(nums[0])):
                    continue
                rid, from_node, to_node = int(nums[1]), int(nums[2]), int(nums[3])
                length_km = nums[7] if len(nums) > 7 else 0.0
            else:
                # Named reach: seq  name  from  to  flags... length
                # (name skipped → from=nums[1], to=nums[2], length=nums[6])
                # Coordinate lines have very few values — filter with len check.
                if len(nums) < 8 or nums[0] != float(int(nums[0])) or int(nums[0]) < 1:
                    continue
                rid, from_node, to_node = int(nums[0]), int(nums[1]), int(nums[2])
                length_km = nums[6] if len(nums) > 6 else 0.0
        else:
            # Old format: seq  from  to  flags... length
            if nums[0] != float(int(nums[0])) or int(nums[0]) < 1:
                continue
            rid, from_node, to_node = int(nums[0]), int(nums[1]), int(nums[2])
            length_km = nums[6] if len(nums) > 6 else 0.0

        reaches[rid] = {
            'from_node': from_node,
            'to_node': to_node,
            'length_km': length_km,
        }

    # node → (downstream_node, reach_length_km, reach_id)
    node_dn = {}
    for rid, r in reaches.items():
        node_dn[r['from_node']] = (r['to_node'], r['length_km'], rid)

    # ── Control vector ────────────────────────────────────────────────────────
    # All .catg files start with: file-title line, then a control-vector header
    # number ("0" or "1").  Skip both before reading instructions.
    rows = []
    _hdr_skip = 0
    calc_no = 0
    _cv_basin_idx = 0   # tracks which basin node corresponds to each Code 1/2 instruction
    j = 0
    while j < len(lines):
        s = lines[j].strip()
        j += 1
        if s.startswith('C') or not s:
            continue
        if _hdr_skip < 2:
            _hdr_skip += 1
            continue   # skip file title (1st) and control-vector header (2nd)
        parts = [p.strip() for p in s.split(',')]
        code_str = parts[0]
        try:
            code_f = float(code_str)
        except ValueError:
            continue
        if code_f == 0:
            break
        calc_no += 1

        row = {
            'calc_no': calc_no,
            'code_str': code_str,
            'node': '', 'reach': '', 'storage': '', 'io': '',
            'name': '', 'io_id': '', 'av_dist_km': None,
            'reach_len': 0.0,   # reach length from instruction field 2
        }

        comment = parts[3] if len(parts) > 3 else ''

        if code_f in (1.0, 2.0):
            try:
                # Two .catg formats exist:
                #   Old: code, length, -99, comment   → parts[2] == '-99'
                #   New: code, coefficient, length, -99 → parts[2] is the length
                if len(parts) > 2 and parts[2].strip() == '-99':
                    row['reach_len'] = float(parts[1]) if len(parts) > 1 else 0.0
                else:
                    row['reach_len'] = float(parts[2]) if len(parts) > 2 else 0.0
            except (ValueError, IndexError):
                pass
            m = re.search(r'Reach\s+(\d+)', comment, re.I)
            if m:
                row['reach'] = m.group(1)
            m = re.search(r'node\s+(\d+)', comment, re.I)
            if m:
                row['node'] = m.group(1)
            # Fallback: infer node/reach from basin_order counter when the
            # comment field carries no IDs (e.g. plain "-99" new format).
            if not row['node'] and _cv_basin_idx < len(basin_order):
                nid = basin_order[_cv_basin_idx]
                row['node'] = str(nid)
                if nid in node_dn:
                    row['reach'] = str(node_dn[nid][2])
            _cv_basin_idx += 1
            row['io_id'] = row['node']

        elif code_f == 5.0:
            try:
                if len(parts) > 2 and parts[2].strip() == '-99':
                    row['reach_len'] = float(parts[1]) if len(parts) > 1 else 0.0
                else:
                    row['reach_len'] = float(parts[2]) if len(parts) > 2 else 0.0
            except (ValueError, IndexError):
                pass
            m = re.search(r'Reach\s+(\d+)', comment, re.I)
            if m:
                row['reach'] = m.group(1)

        elif 7.0 <= code_f < 8.0:
            # Name is on the next non-blank, non-comment line.
            # Real instructions always contain a comma; the terminator is bare "0".
            # Bare numbers without a comma are ISA group labels (e.g. "1", "2"),
            # NOT instructions — consume them as the node name.
            while j < len(lines):
                ns = lines[j].strip()
                j += 1
                if ns and not ns.startswith('C'):
                    candidate = ns.split(',')[0].strip()
                    if ',' in ns or candidate == '0':
                        j -= 1  # real instruction or terminator — put back
                    else:
                        row['name'] = candidate  # text name or bare ISA label
                    break
            for nid, n in nodes.items():
                if n['name'] == row['name']:
                    row['node'] = str(nid)
                    break
            # Fallback for unnamed prints: assign from the #NODES queue
            if not row['node']:
                q = _unnamed_q72 if code_f > 7.05 else _unnamed_q70
                if q:
                    row['node'] = str(q.popleft())
            row['code_str'] = '7'          # display both as "7"
            row['_resets_isa'] = code_f > 7.05  # 7.2 resets ISA; plain 7 does not

        elif code_f == 9.0:
            m = re.search(r'(\d+)', comment)
            if m:
                row['io'] = m.group(1)

        rows.append(row)

    # ── Area assignment: CV Code-1 order, not #NODES order ───────────────────
    # The "Areas, km²" data table lists areas in the order Code-1/2 instructions
    # appear in the control vector, NOT in #NODES appearance order.
    area_vals = _parse_data_table(lines, 'Areas, km')
    cv_basin_order = [int(r['node']) for r in rows
                      if r['code_str'] in ('1', '2') and r['node']]
    for idx, nid in enumerate(cv_basin_order):
        if idx < len(area_vals) and nid in nodes:
            nodes[nid]['area'] = area_vals[idx]

    # ── Av. Dist. via RORB state-machine simulation ───────────────────────────
    # RORB computes Av. Dist. by tracking (av_dist, area) through the control
    # vector, mirroring its internal stack-based hydrograph accumulation:
    #   Code 1: H_run = (reach_len, sub_area_area)           replace
    #   Code 2: H_run = combine(H_run, (reach_len, area))    add to current
    #   Code 3: stack.push(H_run)   [H_run unchanged]
    #   Code 4: H_run = combine(H_run, stack.pop())
    #   Code 5: H_run.av_dist += reach_len
    #   Code 7: record H_run.av_dist; H_run = (0, 0)         reset

    def _wt_combine(av1, a1, av2, a2):
        total = a1 + a2
        if total <= 0:
            return 0.0, 0.0
        return (av1 * a1 + av2 * a2) / total, total

    h_av, h_a = 0.0, 0.0   # running (av_dist, area)
    sim_stack = []          # list of (av_dist, area)

    for row in rows:
        cs = row['code_str']
        if cs == '1':
            if row['node']:
                try:
                    area = nodes[int(row['node'])]['area']
                    h_av, h_a = row['reach_len'], area
                except (ValueError, KeyError):
                    pass
        elif cs == '2':
            if row['node']:
                try:
                    area = nodes[int(row['node'])]['area']
                    # The existing running hydrograph also travels reach_len before
                    # combining with the new sub-area, so advance h_av first.
                    h_av, h_a = _wt_combine(h_av + row['reach_len'], h_a,
                                            row['reach_len'], area)
                except (ValueError, KeyError):
                    pass
        elif cs == '3':
            sim_stack.append((h_av, h_a))
        elif cs == '4':
            if sim_stack:
                s_av, s_a = sim_stack.pop()
                h_av, h_a = _wt_combine(h_av, h_a, s_av, s_a)
        elif cs == '5':
            h_av += row['reach_len']
        elif cs == '7':
            if row['node'] and h_a > 0:
                row['av_dist_km'] = h_av
            if row.get('_resets_isa', False):
                h_av, h_a = 0.0, 0.0   # Code 7.2 only; plain Code 7 keeps H_run

    # ── Missing elements ──────────────────────────────────────────────────────
    missing = {'nodes': [], 'reaches': [], 'storages': [], 'io': []}
    for row in rows:
        if row['node'] and row['code_str'] != '7':
            try:
                nid = int(row['node'])
                if nid not in nodes and nid not in missing['nodes']:
                    missing['nodes'].append(nid)
            except ValueError:
                pass
        if row['reach']:
            try:
                rid = int(row['reach'])
                if rid not in reaches and rid not in missing['reaches']:
                    missing['reaches'].append(rid)
            except ValueError:
                pass

    missing = {k: [str(x) for x in sorted(v)] for k, v in missing.items()}
    return rows, missing


# ── .par file writer ────────────────────────────────────────────────────────

def write_par_file(path, catg, stm, lumped, verbosity, lossmodel, areas_params):
    """
    Write a RORB_CMD # BEGIN...# END parameter file.
    areas_params: list of dicts with keys kc, m, il, cl.
    """
    lines = ['# BEGIN',
             f'Cat file :{catg}',
             f'Stm file :{stm}',
             f'Lumped kc:{"T" if lumped else "F"}',
             f'Verbosity:{verbosity}',
             f'Lossmodel:{lossmodel}',
             f'Num ISA  :{len(areas_params)}']

    if lumped:
        kc, m = areas_params[0]['kc'], areas_params[0]['m']
        lines.append(f'ISA 1    :{kc:.4f},{m:.4f}')
    else:
        for i, p in enumerate(areas_params, 1):
            lines.append(f'ISA {i:<5}:{p["kc"]:.4f},{p["m"]:.4f}')

    lines.append('Num burst:1')
    for i, p in enumerate(areas_params, 1):
        lines.append(f'ISA {i:<5}:{p["il"]:.4f},{p["cl"]:.4f}')

    lines.append('# END')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


# ── Output file discovery ───────────────────────────────────────────────────

def _newest_file_since(directory, ext, since_ts):
    candidates = []
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for fn in names:
        if not fn.lower().endswith(ext):
            continue
        full = os.path.join(directory, fn)
        try:
            if os.path.getmtime(full) >= since_ts:
                candidates.append(full)
        except OSError:
            pass
    return max(candidates, key=os.path.getmtime) if candidates else None


# ── AEP / Duration lists ─────────────────────────────────────────────────────

_AEPS = [
    '12EY', '6EY', '4EY', '3EY', '2EY', '63.2%', '50%', '0.5EY',
    '20%', '10%', '5%', '2%', '1%', '0.2EY',
    '1 in 200', '1 in 500', '1 in 1000', '1 in 2000',
]

_DURATIONS = [
    '1 min', '2 min', '3 min', '5 min', '10 min', '15 min', '30 min',
    '1 hr', '2 hr', '3 hr', '6 hr', '12 hr', '24 hr',
    '48 hr', '72 hr', '96 hr', '120 hr', '144 hr', '168 hr',
]

_DUR_TO_MIN = {
    '1 min': 1, '2 min': 2, '3 min': 3, '5 min': 5, '10 min': 10,
    '15 min': 15, '30 min': 30, '1 hr': 60, '2 hr': 120, '3 hr': 180,
    '6 hr': 360, '12 hr': 720, '24 hr': 1440, '48 hr': 2880, '72 hr': 4320,
    '96 hr': 5760, '120 hr': 7200, '144 hr': 8640, '168 hr': 10080,
}


# ── Background worker — single .stm run ─────────────────────────────────────

class _RunWorker(QThread):
    done = pyqtSignal(bool, str, str)   # ok, log_text, out_file_path

    def __init__(self, exe, par_path, watch_dir, since_ts, timeout=300):
        super().__init__()
        self.exe = exe
        self.par_path = par_path
        self.watch_dir = watch_dir
        self.since_ts = since_ts
        self.timeout = timeout

    def run(self):
        try:
            r = subprocess.run([self.exe, self.par_path],
                               capture_output=True, text=True,
                               timeout=self.timeout)
            text = (r.stdout or '') + (r.stderr or '')
        except Exception as e:
            self.done.emit(False, f'[RORB_CMD failed to launch: {e}]', '')
            return

        out_file = _newest_file_since(self.watch_dir, '.out', self.since_ts)
        log_file = _newest_file_since(self.watch_dir, '.log', self.since_ts)

        log_text = ''
        if log_file and os.path.isfile(log_file):
            try:
                with open(log_file, encoding='utf-8', errors='replace') as f:
                    log_text = f.read().strip()
            except OSError:
                pass

        ok = bool(out_file) and not log_text

        full_text = text
        if log_text:
            full_text += f'\n\n[RORB log file: {log_file}]\n{log_text}'
        self.done.emit(ok, full_text, out_file or '')


# ── Background worker — ARR2016 ensemble ────────────────────────────────────

class _EnsembleWorker(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(bool, str, int, int)  # ok, summary_csv_path, n_ok, n_total

    def __init__(self, exe, catg_path, depths_csv, rinc_csv, hub_txt,
                 aep_list, dur_min_list, out_dir, stm_dir=None,
                 lumped=True, verbosity=None, lossmodel=None, areas_params=None,
                 arf_area_km2=None):
        super().__init__()
        self.exe = exe
        self.catg_path = catg_path
        self.depths_csv = depths_csv
        self.rinc_csv = rinc_csv
        self.hub_txt = hub_txt          # may be None (ARF will be 1.0)
        self.aep_list = aep_list
        self.dur_min_list = dur_min_list
        self.out_dir = out_dir
        self.stm_dir = stm_dir or out_dir  # separate folder for .stm reuse
        self.lumped = lumped
        self.verbosity = verbosity
        self.lossmodel = lossmodel
        self.areas_params = areas_params
        self.arf_area_km2 = arf_area_km2  # override area for ARF calc (None = use catg total)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        os.makedirs(self.out_dir, exist_ok=True)
        workspace = tempfile.mkdtemp(prefix='rorb_ens_')
        try:
            self._execute(workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _execute(self, workspace):
        # Copy catg to workspace (handles non-ASCII paths transparently)
        catg_ws = os.path.join(workspace, os.path.basename(self.catg_path))
        try:
            shutil.copy2(self.catg_path, catg_ws)
        except OSError as e:
            self.progress.emit(f'[ERROR] Cannot copy .catg to workspace: {e}')
            self.done.emit(False, '', 0, 0)
            return

        # Load input data
        try:
            depths, _ = _arr.load_depths(self.depths_csv)
        except Exception as e:
            self.progress.emit(f'[ERROR] Cannot load IFD depths: {e}')
            self.done.emit(False, '', 0, 0)
            return
        try:
            patterns = _arr.load_patterns(self.rinc_csv)
        except Exception as e:
            self.progress.emit(f'[ERROR] Cannot load temporal patterns: {e}')
            self.done.emit(False, '', 0, 0)
            return

        arf_params = None
        if self.hub_txt:
            try:
                arf_params = _arr.load_arf_params(self.hub_txt)
            except Exception:
                pass  # ARF will be 1.0

        # Catchment area for ARF and ISA count for .stm sub-area depths
        _catg_areas = parse_catg_areas(catg_ws)
        area_km2 = sum(a['area_km2'] for a in _catg_areas)
        n_subareas = max(1, len(_catg_areas))
        # arf_area: area used for ARF calculation (may be overridden by user)
        arf_area = self.arf_area_km2 if self.arf_area_km2 else area_km2

        # Restrict to durations that appear in both CSVs
        common_durs = sorted(
            set(self.dur_min_list) & set(depths.keys()) & {k[0] for k in patterns}
        )
        if not common_durs:
            self.progress.emit('[ERROR] No common durations between IFD CSV and temporal patterns CSV.')
            self.done.emit(False, '', 0, 0)
            return

        # Emit diagnostics so mismatches are visible in the log
        depths_aeps = sorted({a for d in depths.values() for a in d})
        pat_classes  = sorted({k[1] for k in patterns})
        self.progress.emit(f'[DEBUG] IFD AEPs in CSV   : {depths_aeps}')
        self.progress.emit(f'[DEBUG] Pattern classes    : {pat_classes}')
        self.progress.emit(f'[DEBUG] Selected AEPs      : {self.aep_list}')
        self.progress.emit(f'[DEBUG] Common durations   : {common_durs} min')

        # Build run list
        runs = []
        for aep in self.aep_list:
            cls = _arr.aep_to_class(aep)
            if cls is None:
                self.progress.emit(f'[WARN] AEP "{aep}" not recognised — skipped')
                continue
            for dur_min in common_durs:
                depth_mm = depths.get(dur_min, {}).get(aep)
                if depth_mm is None:
                    self.progress.emit(
                        f'[WARN] No depth for AEP "{aep}" at {dur_min} min — '
                        f'available AEPs at this duration: '
                        f'{sorted(depths.get(dur_min, {}).keys())}')
                    continue
                arf = _arr.calc_arf(arf_params, dur_min, arf_area,
                                    _arr.aep_label_to_fraction(aep))
                key = (dur_min, cls)
                pats = sorted(patterns.get(key, []), key=lambda x: x[0])
                # Some regional CSVs omit 'very rare' — fall back to 'rare' patterns
                if not pats and cls == 'very rare':
                    pats = sorted(patterns.get((dur_min, 'rare'), []), key=lambda x: x[0])
                if not pats:
                    self.progress.emit(
                        f'[WARN] No temporal patterns for class "{cls}" at {dur_min} min — '
                        f'available classes at this duration: '
                        f'{sorted(v[1] for v in patterns if v[0] == dur_min)}')
                for pos, (event_id, ts_min, fracs) in enumerate(pats, 1):
                    tp_num = _arr.tp_number(aep, pos)
                    runs.append({
                        'aep': aep, 'cls': cls, 'dur_min': dur_min,
                        'tp_num': tp_num, 'event_id': event_id,
                        'ts_min': ts_min, 'fracs': fracs,
                        'raw_depth': depth_mm,
                        'depth_mm': depth_mm * arf, 'arf': arf,
                    })

        total = len(runs)
        if total == 0:
            self.progress.emit('[WARN] No runs — check AEP/duration selections match CSV data.')
            self.done.emit(False, '', 0, 0)
            return

        catg_stem = os.path.splitext(os.path.basename(catg_ws))[0]
        results = []
        n_ok = 0
        t0 = time.time()

        for run_i, run in enumerate(runs):
            if self._stop:
                break

            aep = run['aep']
            dur_min = run['dur_min']
            tp_num = run['tp_num']
            event_id = run['event_id']
            aep_s = _arr.aep_filename_label(aep)
            dur_s = _arr.dur_label(dur_min)
            base = f' aep{aep_s}_du{dur_s}tp{tp_num}'  # space before aep, no _ before tp

            stm_path = os.path.join(workspace, base + '.stm')
            par_path = os.path.join(workspace, base + '.par')

            _arr.write_stm(
                stm_path, run['fracs'], run['ts_min'],
                run['raw_depth'], run['arf'],
                catg_path=self.catg_path,
                catg_name=catg_stem,
                aep=aep,
                dur_display_str=_arr.dur_display(dur_min),
                tp_num=tp_num,
                area_km2=arf_area,
                n_subareas=n_subareas,
            )
            write_par_file(par_path, catg_ws, stm_path,
                           self.lumped, self.verbosity, self.lossmodel,
                           self.areas_params)

            try:
                subprocess.run([self.exe, par_path],
                               capture_output=True, timeout=60)
            except Exception:
                pass

            expected_out = os.path.join(workspace, f'{catg_stem}_{base}.out')
            ok = os.path.exists(expected_out)
            node_peaks = _arr.parse_all_peaks(expected_out) if ok else []
            peak = max((v for _, v in node_peaks), default=None)

            out_name = f'{catg_stem}_{base}'
            if ok:
                n_ok += 1
                try:
                    shutil.copy2(expected_out,
                                 os.path.join(self.out_dir, out_name + '.out'))
                except OSError:
                    pass
            # Always save .stm to stm_dir (may differ from out_dir)
            try:
                os.makedirs(self.stm_dir, exist_ok=True)
                shutil.copy2(stm_path,
                             os.path.join(self.stm_dir, out_name + '.stm'))
            except OSError:
                pass

            results.append({
                'aep': aep, 'dur_min': dur_min, 'tp_num': tp_num, 'event_id': event_id,
                'depth_mm': run['depth_mm'], 'arf': run['arf'],
                'peak': peak, 'node_peaks': node_peaks, 'ok': ok,
            })

            # Clean up workspace temp files for this run
            for p in [stm_path, par_path, expected_out,
                      expected_out.replace('.out', '.log')]:
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

            # Emit progress line
            elapsed = time.time() - t0
            pct = (run_i + 1) / total * 100
            eta = elapsed / (run_i + 1) * (total - run_i - 1) if run_i > 0 else 0
            status = 'OK  ' if ok else 'FAIL'
            peak_s = f'{peak:.1f}' if peak is not None else 'None'
            self.progress.emit(
                f'[{run_i + 1}/{total} {pct:.0f}%]  {base}: {status}  '
                f'depth={run["depth_mm"]:.1f}mm  peak={peak_s} m3/s  ETA={eta:.0f}s'
            )

        # ── Summary files ────────────────────────────────────────────────────
        summary_path = ''

        # Discover node names from the first successful .out file
        node_legend = []  # [(num_str, name), ...]
        for r in results:
            if r['ok'] and r['node_peaks']:
                # Try to get full legend from the copied .out file in out_dir
                aep_s_ = _arr.aep_filename_label(r['aep'])
                dur_s_ = _arr.dur_label(r['dur_min'])
                fn = f'{catg_stem}_ aep{aep_s_}_du{dur_s_}tp{r["tp_num"]}.out'
                legend_path = os.path.join(self.out_dir, fn)
                node_legend = _arr.parse_node_legend(legend_path)
                if node_legend:
                    break
        # Fall back: number the nodes from the first result's node_peaks
        if not node_legend:
            for r in results:
                if r['node_peaks']:
                    node_legend = [(f'{i+1:02d}', name)
                                   for i, (name, _) in enumerate(r['node_peaks'])]
                    break
        n_nodes = len(node_legend) if node_legend else 1

        # 1. Batch .out — fixed-width text matching RORBWin batch format
        batch_out_path = os.path.join(self.out_dir, f'{catg_stem}_batch.out')
        try:
            kc0 = self.areas_params[0]['kc'] if self.areas_params else 0.0  # noqa: E501
            m0  = self.areas_params[0]['m']  if self.areas_params else 0.0
            il0 = self.areas_params[0]['il'] if self.areas_params else 0.0
            cl0 = self.areas_params[0]['cl'] if self.areas_params else 0.0
            # (areas_params is the worker's own attribute — set in __init__)

            peak_hdr = ''.join(f'  Peak{num:>04s}' for num, _ in node_legend) if node_legend \
                       else '  Peak_max'

            # Assign Run number per unique (AEP, Duration)
            run_nums = {}
            run_ctr = 0
            for r in results:
                k = (r['aep'], r['dur_min'])
                if k not in run_nums:
                    run_ctr += 1
                    run_nums[k] = run_ctr

            with open(batch_out_path, 'w', encoding='utf-8') as f:
                f.write(' RORBWin Batch Run Summary\n')
                f.write(' *************************\n\n')
                f.write(f' Date run: {time.strftime("%d %b %Y %H:%M")}\n\n')
                f.write(f' Catchment file   : {self.catg_path}\n')
                f.write(' Rainfall location: User defined\n')
                f.write(' Temporal pattern : ARR2016 point temporal patterns\n')
                f.write(' Spatial pattern  : Uniform\n')
                f.write(' Areal Red. Fact. : Based on ARR 2016 (Book 2 Chapter 4)\n')
                f.write(' Loss factors     : Constant with ARI\n\n\n')
                f.write(f' Parameters:  kc = {kc0:8.2f}    m = {m0:.2f}\n\n')
                f.write(' Loss parameters     Initial loss (mm)   Cont. loss (mm/h)\n')
                f.write(f'                          {il0:>8.2f}              {cl0:.2f}\n\n')
                if node_legend:
                    f.write(' Peak  Description\n')
                    for num, name in node_legend:
                        f.write(f'   {num}  Calculated hydrograph,  {name}\n')
                    f.write(' \n')
                f.write(f' Run        Duration             AEP   TPat  Rain(mm)     ARF{peak_hdr}\n')
                for r in results:
                    rn = run_nums[(r['aep'], r['dur_min'])]
                    dur_disp = _arr.dur_display(r['dur_min'])
                    if r['ok'] and r['node_peaks']:
                        peak_vals = ''.join(f'{v:>10.4f}' for _, v in r['node_peaks'])
                    elif r['ok'] and r['peak'] is not None:
                        peak_vals = f'{r["peak"]:>10.4f}'
                    else:
                        peak_vals = ''.join('    ------' for _ in range(max(n_nodes, 1)))
                    f.write(f' {rn:>3}  {dur_disp:>16}  {r["aep"]:>10}  {r["tp_num"]:>5}'
                            f'  {r["depth_mm"]:>8.2f}  {r["arf"]:>6.2f}{peak_vals}\n')
            summary_path = batch_out_path
        except OSError as e:
            self.progress.emit(f'[WARN] Could not write batch .out: {e}')

        # 2. Critical peaks CSV — one row per (AEP, Duration)
        from collections import defaultdict as _dd
        _ad_peaks = _dd(list)
        for r in results:
            if r['ok'] and r['peak'] is not None:
                _ad_peaks[(r['aep'], r['dur_min'])].append((r['peak'], r['tp_num']))
        crit_path = os.path.join(self.out_dir, f'{catg_stem}_critical_peaks.csv')
        try:
            with open(crit_path, 'w', newline='') as f:
                w = _csv_mod.writer(f)
                w.writerow(['AEP', 'Duration', 'Duration_min', 'Depth_mm', 'ARF',
                            'Max_peak_m3s', 'Critical_tp', 'n_ok', 'n_runs'])
                seen = {}
                for r in results:
                    k = (r['aep'], r['dur_min'])
                    if k not in seen:
                        seen[k] = {'depth': r['depth_mm'], 'arf': r['arf'],
                                   'n_ok': 0, 'n_runs': 0}
                    seen[k]['n_runs'] += 1
                    if r['ok']:
                        seen[k]['n_ok'] += 1
                for (aep, dur_min), meta in seen.items():
                    peaks = _ad_peaks.get((aep, dur_min), [])
                    max_peak, crit_tp = max(peaks, key=lambda x: x[0]) if peaks else (None, None)
                    w.writerow([aep, _arr.dur_display(dur_min), dur_min,
                                round(meta['depth'], 2), round(meta['arf'], 4),
                                max_peak, crit_tp, meta['n_ok'], meta['n_runs']])
        except OSError as e:
            self.progress.emit(f'[WARN] Could not write critical peaks: {e}')

        elapsed = time.time() - t0
        self.progress.emit(f'Done: {n_ok}/{total} successful in {elapsed:.0f}s')
        self.progress.emit(f'Outputs: {self.out_dir}')
        if summary_path:
            self.progress.emit(f'Batch .out: {batch_out_path}')
            self.progress.emit(f'Critical peaks: {crit_path}')
        self.done.emit(n_ok > 0, summary_path, n_ok, total)


class _StmFolderWorker(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(bool, str, int, int)  # ok, out_dir, n_ok, n_total

    def __init__(self, exe, catg_path, stm_folder, out_dir,
                 lumped=True, verbosity=None, lossmodel=None, areas_params=None):
        super().__init__()
        self.exe = exe
        self.catg_path = catg_path
        self.stm_folder = stm_folder
        self.out_dir = out_dir
        self.lumped = lumped
        self.verbosity = verbosity
        self.lossmodel = lossmodel
        self.areas_params = areas_params
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        workspace = tempfile.mkdtemp(prefix='rorb_stmf_')
        try:
            self._execute(workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _execute(self, workspace):
        stm_files = sorted(f for f in os.listdir(self.stm_folder)
                           if f.lower().endswith('.stm'))
        if not stm_files:
            self.progress.emit('[ERROR] No .stm files found in the selected folder.')
            self.done.emit(False, '', 0, 0)
            return

        catg_ws = os.path.join(workspace, os.path.basename(self.catg_path))
        try:
            shutil.copy2(self.catg_path, catg_ws)
        except OSError as e:
            self.progress.emit(f'[ERROR] Cannot copy .catg to workspace: {e}')
            self.done.emit(False, '', 0, 0)
            return

        os.makedirs(self.out_dir, exist_ok=True)
        catg_stem = os.path.splitext(os.path.basename(catg_ws))[0]
        n_total = len(stm_files)
        n_ok = 0
        t0 = time.time()

        for i, stm_name in enumerate(stm_files):
            if self._stop:
                break

            stm_stem = os.path.splitext(stm_name)[0]
            stm_ws = os.path.join(workspace, stm_name)
            try:
                shutil.copy2(os.path.join(self.stm_folder, stm_name), stm_ws)
            except OSError as e:
                self.progress.emit(f'  [{i+1}/{n_total}] ✗ {stm_stem} — copy error: {e}')
                continue

            par_path = os.path.join(workspace, stm_stem + '.par')
            write_par_file(par_path, catg_ws, stm_ws,
                           self.lumped, self.verbosity, self.lossmodel,
                           self.areas_params)

            before_outs = {f for f in os.listdir(workspace) if f.lower().endswith('.out')}
            try:
                subprocess.run([self.exe, par_path], capture_output=True, timeout=60)
            except Exception:
                pass
            new_outs = {f for f in os.listdir(workspace)
                        if f.lower().endswith('.out')} - before_outs

            ok = bool(new_outs)
            if ok:
                n_ok += 1
                for out_fn in new_outs:
                    try:
                        shutil.copy2(os.path.join(workspace, out_fn),
                                     os.path.join(self.out_dir, stm_stem + '.out'))
                    except OSError:
                        pass

            elapsed = time.time() - t0
            tag = '✓' if ok else '✗'
            self.progress.emit(f'  [{i+1}/{n_total}] {tag} {stm_stem}  ({elapsed:.0f}s elapsed)')

            for p in ([stm_ws, par_path]
                      + [os.path.join(workspace, f) for f in new_outs]
                      + [os.path.join(workspace, f.replace('.out', '.log')) for f in new_outs]):
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

        self.done.emit(n_ok > 0, self.out_dir, n_ok, n_total)


# ── Dialog ───────────────────────────────────────────────────────────────────

class RorbRunDialog(QDialog):

    def __init__(self, iface, parent=None, catg_path=None, stm_path=None,
                 on_open_results=None):
        super().__init__(parent)
        self.iface = iface
        self._on_open_results = on_open_results

        self._areas = []
        self._isa_count = 1
        self._isa_names = ['ISA 1']
        self._worker = None           # _RunWorker for single .stm run
        self._ens_worker = None       # _EnsembleWorker for ARR2016 ensemble
        self._dur_to_min = dict(_DUR_TO_MIN)  # updated when temporal CSV is loaded
        self._last_out_file = None    # path to .out from last single run
        self._last_out_folder = None  # output folder from last ensemble run
        self._ens_summary_path = ''

        # staging state for single run
        self._staging_dir = None
        self._staging_inputs = set()
        self._original_output_dir = None

        self._ifd_dur_mins = set()    # durations (int, minutes) present in IFD CSV
        self._tp_dur_mins = set()     # durations (int, minutes) present in temporal patterns CSV

        self._run_mode = 'text'       # 'text' or 'plot'
        self._storm_mode = 'stm'      # 'stm' or 'arr2016'
        self._run_setup = None        # setup snapshot captured at run start
        self._calc_order_rows = []    # Code 7 rows for Suggest Kc av. dist lookup
        self._last_peak_m3s = None    # peak flow from last successful single run

        self._setup_ui()
        self._restore_settings()

        if catg_path:
            self.txt_catg.setText(catg_path)
            self._load_catg(catg_path)
        if stm_path:
            self.txt_stm.setText(stm_path)

    def closeEvent(self, event):
        self._save_settings()
        # Stop any running workers
        for w in (self._worker, self._ens_worker):
            if w and w.isRunning():
                if hasattr(w, 'stop'):
                    w.stop()
                w.wait(2000)
        super().closeEvent(event)

    # ── Settings persistence ─────────────────────────────────────────────────

    def _save_settings(self):
        s = QSettings('RORB', 'RunDialog')
        s.setValue('exe_path', self.txt_exe.text())
        s.setValue('out_dir', self.txt_out_dir.text())
        s.setValue('catg_path', self.txt_catg.text())
        s.setValue('stm_path', self.txt_stm.text())
        s.setValue('hub_path', self.txt_hub.text())
        s.setValue('ifd_path', self.txt_ifd.text())
        s.setValue('temporal_path', self.txt_temporal.text())
        s.setValue('stm_dir', self.txt_stm_dir.text())
        s.setValue('stm_input_folder', self.txt_stm_input_folder.text())
        s.setValue('arf_area_override', self.chk_arf_area.isChecked())
        s.setValue('arf_area_km2',      self.spn_arf_area.value())
        s.setValue('storm_mode', self._storm_mode)
        s.setValue('aep_from', self.cmb_aep_from.currentText())
        s.setValue('aep_to', self.cmb_aep_to.currentText())
        s.setValue('dur_from', self.cmb_dur_from.currentText())
        s.setValue('dur_to', self.cmb_dur_to.currentText())

    def _restore_settings(self):
        s = QSettings('RORB', 'RunDialog')
        _set = lambda w, k: w.setText(s.value(k, '')) if s.value(k) else None
        _set(self.txt_out_dir, 'out_dir')
        _set(self.txt_stm, 'stm_path')
        _set(self.txt_stm_input_folder, 'stm_input_folder')
        _set(self.txt_hub, 'hub_path')
        _set(self.txt_ifd, 'ifd_path')
        _set(self.txt_temporal, 'temporal_path')
        _set(self.txt_stm_dir, 'stm_dir')
        self.chk_arf_area.setChecked(s.value('arf_area_override', False, type=bool))
        self.spn_arf_area.setValue(s.value('arf_area_km2', 0.0, type=float))
        self.spn_arf_area.setEnabled(self.chk_arf_area.isChecked())

        # Restore exe path (prefer auto-detect if saved path no longer exists)
        saved_exe = s.value('exe_path', '')
        if saved_exe and os.path.isfile(saved_exe):
            self.txt_exe.setText(saved_exe)
        self._update_exe_status()

        # Restore catg (and trigger table build)
        catg = s.value('catg_path', '')
        if catg and os.path.isfile(catg):
            self.txt_catg.setText(catg)
            self._load_catg(catg)

        # Restore AEP/duration range
        for cmb, key in [(self.cmb_aep_from, 'aep_from'), (self.cmb_aep_to, 'aep_to'),
                         (self.cmb_dur_from, 'dur_from'), (self.cmb_dur_to, 'dur_to')]:
            v = s.value(key, '')
            if v and cmb.findText(v) >= 0:
                cmb.setCurrentText(v)

        # Restore storm mode last (so widgets exist)
        saved_mode = s.value('storm_mode', '')
        if saved_mode == 'arr2016':
            self.rd_arr2016.setChecked(True)
            self._on_storm_mode_changed()
        elif saved_mode == 'stm_folder':
            self.rd_stm_folder.setChecked(True)
            self._on_storm_mode_changed()

    # ── Top-level layout ─────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle('Run RORB')
        self.setMinimumWidth(780)
        self.setMinimumHeight(600)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        self.tabs = QTabWidget()
        self._add_pipeline_tab()
        self.tabs.addTab(self._build_tab_calc_order(), 'Calc. Order')
        self.tabs.addTab(self._build_tab_storm(), 'Setup')
        self.tabs.addTab(self._build_tab_params(), 'Parameters')
        self.tabs.addTab(self._build_tab_log(), 'Log')
        root.addWidget(self.tabs)

        # Footer button row
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton('Cancel')
        btn_cancel.clicked.connect(self.close)
        btn_row.addWidget(btn_cancel)
        btn_help = QPushButton('Help')
        btn_help.clicked.connect(self._on_help)
        btn_row.addWidget(btn_help)
        btn_row.addStretch()

        self.btn_open_results = QPushButton('Open in Results Viewer')
        self.btn_open_results.setEnabled(False)
        self.btn_open_results.clicked.connect(self._open_in_results)
        btn_row.addWidget(self.btn_open_results)

        grp_run = QGroupBox('Run Model')
        rm_lay = QHBoxLayout(grp_run)
        rm_lay.setContentsMargins(6, 4, 6, 4)
        self.btn_run_text = QPushButton('Run')
        self.btn_run_text.clicked.connect(lambda: self._trigger_run('text'))
        rm_lay.addWidget(self.btn_run_text)
        self.btn_run_plot = QPushButton('Run + View')
        self.btn_run_plot.setToolTip('Run and automatically open output in Results Viewer')
        self.btn_run_plot.clicked.connect(lambda: self._trigger_run('plot'))
        rm_lay.addWidget(self.btn_run_plot)
        self.btn_stop = QPushButton('Stop')
        self.btn_stop.setToolTip('Stop the ensemble run after the current storm finishes')
        self.btn_stop.clicked.connect(self._on_stop_ensemble)
        self.btn_stop.setVisible(False)
        rm_lay.addWidget(self.btn_stop)
        btn_row.addWidget(grp_run)

        root.addLayout(btn_row)

    # ── Tab 0: Build .catg ───────────────────────────────────────────────────

    def _add_pipeline_tab(self):
        try:
            from .pipeline_dialog import RorbPipelineDialog
            self._pipeline_widget = RorbPipelineDialog(
                iface=self.iface,
                parent=self,
                on_open_results=self._on_open_results,
                on_catg_built=self._on_catg_built_from_tab,
            )
            self.tabs.addTab(self._pipeline_widget, 'Build .catg')
        except Exception:
            self._pipeline_widget = None

    def _on_catg_built_from_tab(self, catg_path):
        self.txt_catg.setText(catg_path)
        self._load_catg(catg_path)
        self.tabs.setCurrentIndex(2)  # Switch to Setup tab

    # ── Tab 1: Calculation Order ─────────────────────────────────────────────

    def _build_tab_calc_order(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # .catg browse row
        catg_row = QHBoxLayout()
        catg_row.addWidget(QLabel('.catg file:'))
        self._calc_txt_catg = QLineEdit()
        self._calc_txt_catg.setPlaceholderText('Select or drag a .catg file to view its calculation order…')
        self._calc_txt_catg.setReadOnly(True)
        catg_row.addWidget(self._calc_txt_catg, 1)
        btn_browse_catg = QPushButton('Browse…')
        btn_browse_catg.setFixedWidth(80)
        btn_browse_catg.clicked.connect(self._browse_calc_catg)
        catg_row.addWidget(btn_browse_catg)
        outer.addLayout(catg_row)

        # Table — Name | Node | Av. Dist. (km)
        col_labels = ['Name', 'Node', 'Av. Dist. (km)']
        self._calc_table = QTableWidget(0, len(col_labels))
        self._calc_table.setHorizontalHeaderLabels(col_labels)
        self._calc_table.verticalHeader().setVisible(False)
        self._calc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._calc_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._calc_table.setAlternatingRowColors(True)
        hh = self._calc_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)          # Name stretches
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # Node
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # Av. Dist.
        outer.addWidget(self._calc_table, 1)

        # Missing-elements status row
        status_row = QHBoxLayout()
        status_row.setSpacing(4)
        for label_text, attr in [
            ('Missing Nodes:', '_lbl_miss_nodes'),
            ('Missing Reaches:', '_lbl_miss_reaches'),
            ('Missing Storages:', '_lbl_miss_storages'),
            ('Missing In/Outflows:', '_lbl_miss_io'),
        ]:
            status_row.addWidget(QLabel(label_text))
            lbl = QLabel('none')
            lbl.setStyleSheet('color: green;')
            setattr(self, attr, lbl)
            status_row.addWidget(lbl)
            status_row.addSpacing(16)
        status_row.addStretch()
        outer.addLayout(status_row)

        self._calc_rows_codes = []
        return w

    def _populate_calc_order_tab(self, path):
        self._calc_table.setRowCount(0)
        self._calc_rows_codes = []
        if not path or not os.path.isfile(path):
            return
        self._calc_txt_catg.setText(path)
        try:
            rows, missing = parse_catg_calc_order(path)
        except Exception:
            return
        self._calc_order_rows = rows

        # Only show Code 7 (print) rows — they're the only ones with name + av. dist.
        print_rows = [row for row in rows if row['code_str'] == '7']
        self._calc_table.setRowCount(len(print_rows))
        COL_NAME = 0; COL_NODE = 1; COL_DIST = 2

        def _item(txt, align=ALIGN_CENTER):
            it = QTableWidgetItem(str(txt) if txt else '')
            it.setTextAlignment(align)
            return it

        for r, row in enumerate(print_rows):
            self._calc_table.setItem(r, COL_NAME,
                _item(row['name'], Qt.AlignLeft | Qt.AlignVCenter))
            self._calc_table.setItem(r, COL_NODE, _item(row['node']))
            av = f'{row["av_dist_km"]:.4f}' if row['av_dist_km'] is not None else ''
            self._calc_table.setItem(r, COL_DIST, _item(av))

        self._calc_rows_codes = []  # no row-level filtering needed

        for attr, key in [
            ('_lbl_miss_nodes', 'nodes'),
            ('_lbl_miss_reaches', 'reaches'),
            ('_lbl_miss_storages', 'storages'),
            ('_lbl_miss_io', 'io'),
        ]:
            lbl = getattr(self, attr)
            items = missing.get(key, [])
            if items:
                lbl.setText(', '.join(items))
                lbl.setStyleSheet('color: red;')
            else:
                lbl.setText('none')
                lbl.setStyleSheet('color: green;')

    def _filter_calc_order_table(self):
        pass  # table now shows only Code 7 rows — no filtering needed

    def _browse_calc_catg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB catchment file', '', 'RORB Graphical (*.catg)')
        if path:
            self._calc_txt_catg.setText(path)
            self._populate_calc_order_tab(path)

    # ── Tab 2: Storm & Run Setup ─────────────────────────────────────────────

    def _build_tab_storm(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        scroll.setWidget(inner)
        vlay = QVBoxLayout(inner)
        vlay.setSpacing(8)

        # Setup save / load
        setup_row = QHBoxLayout()
        btn_save_setup = QPushButton('Save setup…')
        btn_save_setup.clicked.connect(self._save_setup)
        setup_row.addWidget(btn_save_setup)
        btn_load_setup = QPushButton('Load setup…')
        btn_load_setup.clicked.connect(self._load_setup)
        setup_row.addWidget(btn_load_setup)
        setup_row.addStretch()
        vlay.addLayout(setup_row)

        # RORB Executable + Output folder
        grp_exe = QGroupBox('RORB Executable')
        form_exe = QFormLayout(grp_exe)
        form_exe.setLabelAlignment(ALIGN_RIGHT)

        row_exe = QHBoxLayout()
        self.txt_exe = QLineEdit(find_rorb_cmd() or '')
        self.txt_exe.setPlaceholderText('Path to RORB_CMD.exe…')
        self.txt_exe.textChanged.connect(self._update_exe_status)
        row_exe.addWidget(self.txt_exe)
        btn_exe = QPushButton('Browse…'); btn_exe.setFixedWidth(80)
        btn_exe.clicked.connect(self._browse_exe)
        row_exe.addWidget(btn_exe)
        form_exe.addRow('RORB_CMD.exe:', row_exe)

        row_outdir = QHBoxLayout()
        self.txt_out_dir = QLineEdit()
        self.txt_out_dir.setPlaceholderText('Output folder for results (required for ensemble)…')
        row_outdir.addWidget(self.txt_out_dir)
        btn_outdir = QPushButton('Browse…'); btn_outdir.setFixedWidth(80)
        btn_outdir.clicked.connect(self._browse_out_dir)
        row_outdir.addWidget(btn_outdir)
        form_exe.addRow('Output folder:', row_outdir)

        self.lbl_exe_status = QLabel('')
        form_exe.addRow('', self.lbl_exe_status)
        self._update_exe_status()

        vlay.addWidget(grp_exe)

        # Storm Input
        grp_storm = QGroupBox('Storm Input')
        storm_v = QVBoxLayout(grp_storm)

        radio_row = QHBoxLayout()
        self._grp_storm_mode = QButtonGroup(self)
        self.rd_stm = QRadioButton('Use single .stm file')
        self.rd_stm.setChecked(True)
        self._grp_storm_mode.addButton(self.rd_stm, 0)
        radio_row.addWidget(self.rd_stm)
        self.rd_arr2016 = QRadioButton('Generate from ARR2016 data (ensemble)')
        self._grp_storm_mode.addButton(self.rd_arr2016, 1)
        radio_row.addWidget(self.rd_arr2016)
        self.rd_stm_folder = QRadioButton('Run batch from existing .stm folder')
        self._grp_storm_mode.addButton(self.rd_stm_folder, 2)
        radio_row.addWidget(self.rd_stm_folder)
        radio_row.addStretch()
        storm_v.addLayout(radio_row)

        # .stm file row (visible when stm mode)
        self.wdg_stm = QWidget()
        stm_h = QHBoxLayout(self.wdg_stm)
        stm_h.setContentsMargins(0, 2, 0, 2)
        stm_h.addWidget(QLabel('.stm file:'))
        self.txt_stm = QLineEdit()
        self.txt_stm.setPlaceholderText('Select .stm storm file…')
        stm_h.addWidget(self.txt_stm)
        btn_stm = QPushButton('Browse…'); btn_stm.setFixedWidth(80)
        btn_stm.clicked.connect(self._browse_stm)
        stm_h.addWidget(btn_stm)
        storm_v.addWidget(self.wdg_stm)

        # ARR2016 inputs (visible when arr2016 mode)
        self.wdg_arr = QWidget()
        arr_form = QFormLayout(self.wdg_arr)
        arr_form.setContentsMargins(0, 2, 0, 2)
        arr_form.setLabelAlignment(ALIGN_RIGHT)

        row_hub = QHBoxLayout()
        self.txt_hub = QLineEdit()
        self.txt_hub.setPlaceholderText('ARR Data Hub .txt file (optional — provides ARF coefficients)…')
        row_hub.addWidget(self.txt_hub)
        btn_hub = QPushButton('Browse…'); btn_hub.setFixedWidth(80)
        btn_hub.clicked.connect(self._browse_hub)
        row_hub.addWidget(btn_hub)
        arr_form.addRow('Data Hub (.txt):', row_hub)

        row_ifd = QHBoxLayout()
        self.txt_ifd = QLineEdit()
        self.txt_ifd.setPlaceholderText('IFD design depths .csv (depths_*.csv)…')
        self.txt_ifd.textChanged.connect(self._on_ifd_changed)
        row_ifd.addWidget(self.txt_ifd)
        btn_ifd = QPushButton('Browse…'); btn_ifd.setFixedWidth(80)
        btn_ifd.clicked.connect(self._browse_ifd)
        row_ifd.addWidget(btn_ifd)
        arr_form.addRow('IFD depths (.csv):', row_ifd)

        row_tp = QHBoxLayout()
        self.txt_temporal = QLineEdit()
        self.txt_temporal.setPlaceholderText('Temporal patterns *_Increments.csv…')
        self.txt_temporal.textChanged.connect(self._on_temporal_changed)
        row_tp.addWidget(self.txt_temporal)
        btn_tp = QPushButton('Browse…'); btn_tp.setFixedWidth(80)
        btn_tp.clicked.connect(self._browse_temporal)
        row_tp.addWidget(btn_tp)
        arr_form.addRow('Temporal patterns:', row_tp)

        row_aep = QHBoxLayout()
        row_aep.addWidget(QLabel('From:'))
        self.cmb_aep_from = QComboBox(); self.cmb_aep_from.addItems(_AEPS)
        self.cmb_aep_from.setCurrentText('63.2%')
        row_aep.addWidget(self.cmb_aep_from)
        row_aep.addSpacing(10)
        row_aep.addWidget(QLabel('To:'))
        self.cmb_aep_to = QComboBox(); self.cmb_aep_to.addItems(_AEPS)
        self.cmb_aep_to.setCurrentText('1%')
        row_aep.addWidget(self.cmb_aep_to)
        row_aep.addStretch()
        arr_form.addRow('AEP range:', row_aep)

        row_dur = QHBoxLayout()
        row_dur.addWidget(QLabel('From:'))
        self.cmb_dur_from = QComboBox(); self.cmb_dur_from.addItems(_DURATIONS)
        self.cmb_dur_from.setCurrentText('10 min')
        row_dur.addWidget(self.cmb_dur_from)
        row_dur.addSpacing(10)
        row_dur.addWidget(QLabel('To:'))
        self.cmb_dur_to = QComboBox(); self.cmb_dur_to.addItems(_DURATIONS)
        self.cmb_dur_to.setCurrentText('168 hr')
        row_dur.addWidget(self.cmb_dur_to)
        row_dur.addStretch()
        arr_form.addRow('Duration range:', row_dur)

        row_stm_dir = QHBoxLayout()
        self.txt_stm_dir = QLineEdit()
        self.txt_stm_dir.setPlaceholderText('Leave blank to save .stm files alongside .out files…')
        row_stm_dir.addWidget(self.txt_stm_dir)
        btn_stm_dir = QPushButton('Browse…'); btn_stm_dir.setFixedWidth(80)
        btn_stm_dir.clicked.connect(self._browse_stm_dir)
        row_stm_dir.addWidget(btn_stm_dir)
        arr_form.addRow('.stm folder (optional):', row_stm_dir)

        # ARF area override
        arf_row = QHBoxLayout()
        self.chk_arf_area = QCheckBox('Replace total catchment area with value of')
        self.chk_arf_area.stateChanged.connect(
            lambda s: self.spn_arf_area.setEnabled(bool(s)))
        arf_row.addWidget(self.chk_arf_area)
        self.spn_arf_area = QDoubleSpinBox()
        self.spn_arf_area.setRange(0, 999999)
        self.spn_arf_area.setDecimals(2)
        self.spn_arf_area.setValue(0.0)
        self.spn_arf_area.setFixedWidth(100)
        self.spn_arf_area.setSuffix(' km²')
        self.spn_arf_area.setEnabled(False)
        arf_row.addWidget(self.spn_arf_area)
        arf_row.addWidget(QLabel('(for ARF only)'))
        arf_row.addStretch()
        arr_form.addRow('ARF area:', arf_row)

        self.wdg_arr.setVisible(False)
        storm_v.addWidget(self.wdg_arr)

        # .stm folder row (visible when stm_folder mode)
        self.wdg_stm_folder = QWidget()
        stmf_h = QHBoxLayout(self.wdg_stm_folder)
        stmf_h.setContentsMargins(0, 2, 0, 2)
        stmf_h.addWidget(QLabel('.stm folder:'))
        self.txt_stm_input_folder = QLineEdit()
        self.txt_stm_input_folder.setPlaceholderText('Folder containing .stm storm files…')
        stmf_h.addWidget(self.txt_stm_input_folder)
        btn_stmf = QPushButton('Browse…'); btn_stmf.setFixedWidth(80)
        btn_stmf.clicked.connect(self._browse_stm_input_folder)
        stmf_h.addWidget(btn_stmf)
        self.wdg_stm_folder.setVisible(False)
        storm_v.addWidget(self.wdg_stm_folder)

        vlay.addWidget(grp_storm)

        # Catchment
        grp_catg = QGroupBox('Catchment')
        catg_form = QFormLayout(grp_catg)
        catg_form.setLabelAlignment(ALIGN_RIGHT)
        row_catg = QHBoxLayout()
        self.txt_catg = QLineEdit()
        self.txt_catg.setPlaceholderText('Select .catg catchment file…')
        row_catg.addWidget(self.txt_catg)
        btn_catg = QPushButton('Browse…'); btn_catg.setFixedWidth(80)
        btn_catg.clicked.connect(self._browse_catg)
        row_catg.addWidget(btn_catg)
        catg_form.addRow('.catg file:', row_catg)
        vlay.addWidget(grp_catg)

        # Parameter Configuration
        grp_param = QGroupBox('Parameter configuration')
        param_h = QHBoxLayout(grp_param)

        self._grp_routing = QButtonGroup(self)
        lft = QVBoxLayout()
        self.rd_param_single = QRadioButton('Single set of routing parameters for whole model (default)')
        self.rd_param_single.setChecked(True)
        self._grp_routing.addButton(self.rd_param_single, 0)
        lft.addWidget(self.rd_param_single)
        self.rd_param_vary = QRadioButton('Vary routing parameters by interstation area')
        self._grp_routing.addButton(self.rd_param_vary, 1)
        lft.addWidget(self.rd_param_vary)
        lft.addStretch()
        param_h.addLayout(lft)

        self._grp_lossmodel = QButtonGroup(self)
        rgt = QVBoxLayout()
        self.rd_loss_ilcl = QRadioButton('Initial loss / continuing loss model')
        self.rd_loss_ilcl.setChecked(True)
        self._grp_lossmodel.addButton(self.rd_loss_ilcl, 0)
        rgt.addWidget(self.rd_loss_ilcl)
        self.rd_loss_rc = QRadioButton('Runoff coefficient model')
        self._grp_lossmodel.addButton(self.rd_loss_rc, 1)
        rgt.addWidget(self.rd_loss_rc)
        rgt.addStretch()
        param_h.addLayout(rgt)

        btn_refresh_isa = QPushButton('Refresh')
        btn_refresh_isa.setToolTip('Re-read ISA groups from the current .catg file')
        btn_refresh_isa.setFixedWidth(70)
        btn_refresh_isa.clicked.connect(self._refresh_isa)
        far_rgt = QVBoxLayout()
        far_rgt.addWidget(btn_refresh_isa)
        far_rgt.addStretch()
        param_h.addLayout(far_rgt)

        vlay.addWidget(grp_param)

        # Output Options
        grp_out = QGroupBox('Output Options')
        out_form = QFormLayout(grp_out)
        out_form.setLabelAlignment(ALIGN_RIGHT)
        self.cmb_info_detail = QComboBox()
        self.cmb_info_detail.addItems(['Flows & all input data', 'Summary only', 'Minimal'])
        out_form.addRow('Information detail:', self.cmb_info_detail)
        self.chk_text_csv = QCheckBox('Include text and CSV outputs')
        out_form.addRow('', self.chk_text_csv)
        vlay.addWidget(grp_out)

        vlay.addStretch()

        self._grp_storm_mode.buttonClicked.connect(self._on_storm_mode_changed)
        self._grp_routing.buttonClicked.connect(self._on_param_mode_changed)

        return scroll

    # ── Tab 2: Parameters ────────────────────────────────────────────────────

    def _build_tab_params(self):
        w = QWidget()
        vlay = QVBoxLayout(w)
        vlay.setSpacing(8)

        # Save / Load / Suggest buttons
        io_row = QHBoxLayout()
        btn_save_p = QPushButton('Save parameters…')
        btn_save_p.clicked.connect(self._save_params)
        io_row.addWidget(btn_save_p)
        btn_load_p = QPushButton('Load parameters…')
        btn_load_p.clicked.connect(self._load_params)
        io_row.addWidget(btn_load_p)
        io_row.addStretch()
        self.btn_suggest_kc = QPushButton('??')
        self.btn_suggest_kc.setFixedWidth(32)
        self.btn_suggest_kc.setToolTip('Suggest Kc values from regional equations (ARR Book 7)')
        self.btn_suggest_kc.clicked.connect(self._open_suggest_kc)
        io_row.addWidget(self.btn_suggest_kc)
        vlay.addLayout(io_row)

        # Global kc / m (lumped mode only)
        self.wdg_global_km = QWidget()
        km_h = QHBoxLayout(self.wdg_global_km)
        km_h.setContentsMargins(4, 0, 4, 0)
        km_h.addWidget(QLabel('Kc ='))
        self.spn_kc_global = QDoubleSpinBox()
        self.spn_kc_global.setRange(0, 1000); self.spn_kc_global.setDecimals(3)
        self.spn_kc_global.setValue(1.5); self.spn_kc_global.setFixedWidth(80)
        km_h.addWidget(self.spn_kc_global)
        btn_kc_help = QPushButton('??')
        btn_kc_help.setFixedWidth(30)
        btn_kc_help.setToolTip('Kc calibration not yet implemented')
        btn_kc_help.clicked.connect(lambda: QMessageBox.information(
            self, 'Not implemented', 'Kc calibration is not yet implemented.'))
        km_h.addWidget(btn_kc_help)
        km_h.addSpacing(20)
        km_h.addWidget(QLabel('m ='))
        self.spn_m_global = QDoubleSpinBox()
        self.spn_m_global.setRange(0, 5); self.spn_m_global.setDecimals(3)
        self.spn_m_global.setValue(0.8); self.spn_m_global.setFixedWidth(80)
        km_h.addWidget(self.spn_m_global)
        km_h.addStretch()
        vlay.addWidget(self.wdg_global_km)

        # Table: cols [Name(0), kc(1), m(2), IL(3), CL(4)]
        self.table_areas = QTableWidget(0, 5)
        self.table_areas.setHorizontalHeaderLabels(['Area', 'kc', 'm', 'IL (mm)', 'CL (mm/h)'])
        self.table_areas.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table_areas.setMinimumHeight(160)
        vlay.addWidget(self.table_areas)

        vlay.addStretch()

        self._apply_param_mode()
        return w

    # ── Tab 3: Log ───────────────────────────────────────────────────────────

    def _build_tab_log(self):
        w = QWidget()
        vlay = QVBoxLayout(w)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText('RORB output will appear here…')
        self.txt_log.setFont(QFont('Consolas', 9))
        vlay.addWidget(self.txt_log)

        self.lbl_status = QLabel('')
        self.lbl_status.setAlignment(ALIGN_CENTER)
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self.lbl_status.setFont(f)
        vlay.addWidget(self.lbl_status)

        return w

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_storm_mode_changed(self, btn=None):
        is_arr2016 = self.rd_arr2016.isChecked()
        is_stm_folder = self.rd_stm_folder.isChecked()
        if is_arr2016:
            self._storm_mode = 'arr2016'
        elif is_stm_folder:
            self._storm_mode = 'stm_folder'
        else:
            self._storm_mode = 'stm'
        self.wdg_stm.setVisible(self._storm_mode == 'stm')
        self.wdg_arr.setVisible(is_arr2016)
        self.wdg_stm_folder.setVisible(is_stm_folder)
        self.btn_run_plot.setVisible(self._storm_mode == 'stm')
        if self._storm_mode == 'stm':
            self.btn_open_results.setText('Open in Results Viewer')
        else:
            self.btn_open_results.setText('Open Output Folder')

    def _on_param_mode_changed(self, btn=None):
        self._rebuild_table()

    def _apply_param_mode(self):
        lumped = self.rd_param_single.isChecked()
        self.wdg_global_km.setVisible(lumped)
        self.table_areas.setColumnHidden(1, lumped)
        self.table_areas.setColumnHidden(2, lumped)

    def _on_help(self):
        QMessageBox.information(self, 'Help',
                                'See RORB documentation for parameter descriptions.')

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _browse_exe(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB_CMD.exe', '', 'Executable (*.exe)')
        if path:
            self.txt_exe.setText(path)

    def _browse_out_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Select output folder')
        if path:
            self.txt_out_dir.setText(path)

    def _browse_catg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB catchment file', '', 'RORB Graphical (*.catg)')
        if path:
            self.txt_catg.setText(path)
            self._load_catg(path)

    def _browse_stm(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB storm file', '', 'RORB Storm (*.stm)')
        if path:
            self.txt_stm.setText(path)

    def _browse_hub(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select ARR Data Hub file', '', 'Text files (*.txt)')
        if path:
            self.txt_hub.setText(path)

    def _browse_ifd(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select IFD depths CSV', '', 'CSV files (*.csv)')
        if path:
            self.txt_ifd.setText(path)

    def _on_ifd_changed(self, path=''):
        path = path or self.txt_ifd.text().strip()
        if not path or not os.path.isfile(path):
            return
        try:
            depths, aep_cols = _arr.load_depths(path)
        except Exception:
            return
        if not aep_cols:
            return

        # Store IFD durations so temporal changes can intersect with them
        self._ifd_dur_mins = set(depths.keys())

        # Filter to AEPs that have a valid temporal-class mapping
        valid_aeps = [a for a in aep_cols if _arr.aep_to_class(a) is not None]
        if not valid_aeps:
            valid_aeps = aep_cols  # fallback: show all if none recognised

        prev_from = self.cmb_aep_from.currentText()
        prev_to = self.cmb_aep_to.currentText()
        self.cmb_aep_from.blockSignals(True)
        self.cmb_aep_to.blockSignals(True)
        self.cmb_aep_from.clear()
        self.cmb_aep_to.clear()
        self.cmb_aep_from.addItems(valid_aeps)
        self.cmb_aep_to.addItems(valid_aeps)
        if prev_from in valid_aeps:
            self.cmb_aep_from.setCurrentText(prev_from)
        if prev_to in valid_aeps:
            self.cmb_aep_to.setCurrentText(prev_to)
        self.cmb_aep_from.blockSignals(False)
        self.cmb_aep_to.blockSignals(False)

        # Re-run duration intersection if temporal patterns already loaded
        if self._tp_dur_mins:
            self._refresh_dur_combo()

    def _browse_temporal(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Temporal Patterns CSV', '', 'CSV files (*.csv)')
        if path:
            self.txt_temporal.setText(path)

    def _on_temporal_changed(self, path=''):
        path = path or self.txt_temporal.text().strip()
        if not path or not os.path.isfile(path):
            return
        try:
            patterns = _arr.load_patterns(path)
        except Exception:
            return
        self._tp_dur_mins = {k[0] for k in patterns.keys()}
        if not self._tp_dur_mins:
            return
        self._refresh_dur_combo()

    def _refresh_dur_combo(self):
        """Rebuild duration combos from the intersection of IFD and temporal-pattern durations."""
        dur_mins = sorted(self._tp_dur_mins)
        # Intersect with IFD durations when both are loaded
        if self._ifd_dur_mins:
            dur_mins = sorted(self._tp_dur_mins & self._ifd_dur_mins)
        if not dur_mins:
            return

        def _label(m):
            if m < 60:
                return f'{m} min'
            h = m / 60.0
            return f'{int(h)} hr' if h == int(h) else f'{m} min'

        labels = [_label(m) for m in dur_mins]
        self._dur_to_min = dict(zip(labels, dur_mins))

        prev_from = self.cmb_dur_from.currentText()
        prev_to = self.cmb_dur_to.currentText()
        self.cmb_dur_from.blockSignals(True)
        self.cmb_dur_to.blockSignals(True)
        self.cmb_dur_from.clear()
        self.cmb_dur_to.clear()
        self.cmb_dur_from.addItems(labels)
        self.cmb_dur_to.addItems(labels)
        self.cmb_dur_from.setCurrentText(prev_from if prev_from in labels else labels[0])
        self.cmb_dur_to.setCurrentText(prev_to if prev_to in labels else labels[-1])
        self.cmb_dur_from.blockSignals(False)
        self.cmb_dur_to.blockSignals(False)

    def _browse_stm_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Select folder to save .stm files')
        if path:
            self.txt_stm_dir.setText(path)

    def _browse_stm_input_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, 'Select folder containing .stm files',
            self.txt_stm_input_folder.text() or '')
        if path:
            self.txt_stm_input_folder.setText(path)

    def _update_exe_status(self):
        exe = self.txt_exe.text().strip()
        if exe and os.path.isfile(exe):
            self.lbl_exe_status.setText('')
        else:
            self.lbl_exe_status.setText(
                '<span style="color:#c0392b;">RORB_CMD.exe not found — '
                'browse to its location.</span>')

    # ── .catg loading ─────────────────────────────────────────────────────────

    def _refresh_isa(self):
        path = self.txt_catg.text().strip()
        if path and os.path.isfile(path):
            self._load_catg(path)
        else:
            QMessageBox.warning(self, 'No .catg file',
                                'Browse to a .catg file in the Setup tab first.')

    def _load_catg(self, path):
        try:
            self._areas = parse_catg_areas(path)
            self._isa_names = parse_catg_isa_groups(path)
            self._isa_count = len(self._isa_names)
        except Exception as e:
            QMessageBox.warning(self, 'Could not read .catg',
                                f'Failed to parse interstation areas:\n\n{e}')
            self._areas = []
            self._isa_names = ['ISA 1']
            self._isa_count = 1
        self._rebuild_table()
        self._populate_calc_order_tab(path)

    def _rebuild_table(self):
        lumped = self.rd_param_single.isChecked()

        prev_il, prev_cl = 20.0, 2.5
        if self.table_areas.rowCount() > 0:
            w_il = self.table_areas.cellWidget(0, 3)
            w_cl = self.table_areas.cellWidget(0, 4)
            if w_il:
                prev_il = w_il.value()
            if w_cl:
                prev_cl = w_cl.value()

        rows = [{'name': name} for name in self._isa_names] or [{'name': 'ISA 1'}]
        self.table_areas.setRowCount(len(rows))
        for row, area in enumerate(rows):
            name_item = QTableWidgetItem(f'#{row + 1:02d}: {area["name"]}')
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.table_areas.setItem(row, 0, name_item)

            spn_kc = QDoubleSpinBox(); spn_kc.setRange(0, 1000); spn_kc.setDecimals(3); spn_kc.setValue(1.5)
            spn_m = QDoubleSpinBox(); spn_m.setRange(0, 5); spn_m.setDecimals(3); spn_m.setValue(0.8)
            spn_il = QDoubleSpinBox(); spn_il.setRange(0, 1000); spn_il.setDecimals(2); spn_il.setValue(prev_il)
            spn_cl = QDoubleSpinBox(); spn_cl.setRange(0, 1000); spn_cl.setDecimals(2); spn_cl.setValue(prev_cl)

            self.table_areas.setCellWidget(row, 1, spn_kc)
            self.table_areas.setCellWidget(row, 2, spn_m)
            self.table_areas.setCellWidget(row, 3, spn_il)
            self.table_areas.setCellWidget(row, 4, spn_cl)

        self._apply_param_mode()

    def _save_params(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save parameters', '', 'CSV files (*.csv)')
        if not path:
            return
        try:
            with open(path, 'w', newline='') as f:
                w = _csv_mod.writer(f)
                w.writerow(['Name', 'kc', 'm', 'IL_mm', 'CL_mmph'])
                lumped = self.rd_param_single.isChecked()
                kc0 = self.spn_kc_global.value() if lumped else None
                m0 = self.spn_m_global.value() if lumped else None
                for row in range(self.table_areas.rowCount()):
                    name = self.table_areas.item(row, 0).text() if self.table_areas.item(row, 0) else f'Row{row+1}'
                    kc = kc0 if lumped else self.table_areas.cellWidget(row, 1).value()
                    m = m0 if lumped else self.table_areas.cellWidget(row, 2).value()
                    il = self.table_areas.cellWidget(row, 3).value()
                    cl = self.table_areas.cellWidget(row, 4).value()
                    w.writerow([name, kc, m, il, cl])
        except OSError as e:
            QMessageBox.warning(self, 'Save failed', str(e))

    def _load_params(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load parameters', '', 'CSV files (*.csv)')
        if not path:
            return
        try:
            with open(path, newline='') as f:
                reader = _csv_mod.DictReader(f)
                rows = list(reader)
        except OSError as e:
            QMessageBox.warning(self, 'Load failed', str(e))
            return
        lumped = self.rd_param_single.isChecked()
        n = min(len(rows), self.table_areas.rowCount())
        for i, row in enumerate(rows[:n]):
            try:
                if not lumped:
                    if self.table_areas.cellWidget(i, 1):
                        self.table_areas.cellWidget(i, 1).setValue(float(row.get('kc', 1.5)))
                    if self.table_areas.cellWidget(i, 2):
                        self.table_areas.cellWidget(i, 2).setValue(float(row.get('m', 0.8)))
                if self.table_areas.cellWidget(i, 3):
                    self.table_areas.cellWidget(i, 3).setValue(float(row.get('IL_mm', 20.0)))
                if self.table_areas.cellWidget(i, 4):
                    self.table_areas.cellWidget(i, 4).setValue(float(row.get('CL_mmph', 2.5)))
            except (ValueError, TypeError):
                pass
        if lumped and rows:
            try:
                self.spn_kc_global.setValue(float(rows[0].get('kc', 1.5)))
                self.spn_m_global.setValue(float(rows[0].get('m', 0.8)))
            except (ValueError, TypeError):
                pass

    def _open_suggest_kc(self):
        lumped = self.rd_param_single.isChecked()
        total_area = sum(a['area_km2'] for a in self._areas)

        # Build per-row label, area, and av_dist lists
        if lumped:
            row_labels = [f'#{i+1:02d}: {name}' for i, name in enumerate(self._isa_names)]
            areas_km2  = []
            for name in self._isa_names:
                # Approximate: equal share of total area per ISA group
                areas_km2.append(total_area / max(1, len(self._isa_names)))
        else:
            row_labels = [f'#{i+1:02d}: {a["name"]}' for i, a in enumerate(self._areas)]
            areas_km2  = [a['area_km2'] for a in self._areas]

        # Match av_dist_km from calc order Code 7 rows by name
        code7 = [r for r in self._calc_order_rows if r['code_str'] == '7']
        av_dists_km = []
        names_to_match = (self._isa_names if lumped
                          else [a['name'] for a in self._areas])
        for name in names_to_match:
            dist = next(
                (r['av_dist_km'] for r in code7
                 if r['name'] == name and r['av_dist_km'] is not None),
                None)
            # Fallback: outlet (last Code 7 with a distance)
            if dist is None:
                dist = next(
                    (r['av_dist_km'] for r in reversed(code7)
                     if r['av_dist_km'] is not None),
                    None)
            av_dists_km.append(dist)

        dlg = SuggestKcDialog(
            self, row_labels, areas_km2, av_dists_km,
            peak_m3s=self._last_peak_m3s)
        dlg.kc_adopted.connect(self._on_kc_adopted)
        dlg.exec()

    def _on_kc_adopted(self, row_idx, kc_value):
        lumped = self.rd_param_single.isChecked()
        if lumped:
            self.spn_kc_global.setValue(kc_value)
        else:
            w = self.table_areas.cellWidget(row_idx, 1)
            if w:
                w.setValue(kc_value)

    def _collect_area_params(self):
        lumped = self.rd_param_single.isChecked()
        kc0 = self.spn_kc_global.value() if lumped else None
        m0 = self.spn_m_global.value() if lumped else None
        params = []
        for row in range(self.table_areas.rowCount()):
            kc = kc0 if lumped else self.table_areas.cellWidget(row, 1).value()
            m = m0 if lumped else self.table_areas.cellWidget(row, 2).value()
            il = self.table_areas.cellWidget(row, 3).value()
            cl = self.table_areas.cellWidget(row, 4).value()
            params.append({'kc': kc, 'm': m, 'il': il, 'cl': cl})
        return params

    # ── Setup save / load ────────────────────────────────────────────────────

    def _collect_setup(self):
        """Return all current dialog settings as a serialisable dict."""
        lumped = self.rd_param_single.isChecked()
        isa_params = []
        for row in range(self.table_areas.rowCount()):
            name = (self.table_areas.item(row, 0).text()
                    if self.table_areas.item(row, 0) else f'Row{row+1}')
            kc_w = self.table_areas.cellWidget(row, 1)
            m_w  = self.table_areas.cellWidget(row, 2)
            il_w = self.table_areas.cellWidget(row, 3)
            cl_w = self.table_areas.cellWidget(row, 4)
            isa_params.append({
                'name': name,
                'kc':   kc_w.value() if kc_w else 1.5,
                'm':    m_w.value()  if m_w  else 0.8,
                'il':   il_w.value() if il_w else 20.0,
                'cl':   cl_w.value() if cl_w else 2.5,
            })
        return {
            'exe_path':       self.txt_exe.text().strip(),
            'out_dir':        self.txt_out_dir.text().strip(),
            'catg_path':      self.txt_catg.text().strip(),
            'storm_mode':     self._storm_mode,
            'stm_path':       self.txt_stm.text().strip(),
            'stm_folder':     self.txt_stm_input_folder.text().strip(),
            'hub_path':       self.txt_hub.text().strip(),
            'ifd_path':       self.txt_ifd.text().strip(),
            'temporal_path':  self.txt_temporal.text().strip(),
            'stm_dir':        self.txt_stm_dir.text().strip(),
            'arf_area_override': self.chk_arf_area.isChecked(),
            'arf_area_km2':      self.spn_arf_area.value(),
            'aep_from':       self.cmb_aep_from.currentText(),
            'aep_to':         self.cmb_aep_to.currentText(),
            'dur_from':       self.cmb_dur_from.currentText(),
            'dur_to':         self.cmb_dur_to.currentText(),
            'routing_lumped': lumped,
            'loss_ilcl':      self.rd_loss_ilcl.isChecked(),
            'verbosity_index': self.cmb_info_detail.currentIndex(),
            'text_csv':       self.chk_text_csv.isChecked(),
            'kc_global':      self.spn_kc_global.value(),
            'm_global':       self.spn_m_global.value(),
            'isa_params':     isa_params,
        }

    def _apply_setup(self, d):
        """Apply a setup dict to all dialog widgets."""
        def _set(w, key):
            v = d.get(key, '')
            if v:
                w.setText(v)

        _set(self.txt_exe,              'exe_path')
        _set(self.txt_out_dir,          'out_dir')
        _set(self.txt_stm,              'stm_path')
        _set(self.txt_stm_input_folder, 'stm_folder')
        _set(self.txt_hub,              'hub_path')
        _set(self.txt_ifd,              'ifd_path')
        _set(self.txt_temporal,         'temporal_path')
        _set(self.txt_stm_dir,          'stm_dir')
        self.chk_arf_area.setChecked(d.get('arf_area_override', False))
        self.spn_arf_area.setValue(d.get('arf_area_km2', 0.0))
        self.spn_arf_area.setEnabled(self.chk_arf_area.isChecked())

        catg = d.get('catg_path', '')
        if catg and os.path.isfile(catg):
            self.txt_catg.setText(catg)
            self._load_catg(catg)

        mode = d.get('storm_mode', 'stm')
        if mode == 'arr2016':
            self.rd_arr2016.setChecked(True)
        elif mode == 'stm_folder':
            self.rd_stm_folder.setChecked(True)
        else:
            self.rd_stm.setChecked(True)
        self._on_storm_mode_changed()

        if d.get('routing_lumped', True):
            self.rd_param_single.setChecked(True)
        else:
            self.rd_param_vary.setChecked(True)
        self._on_param_mode_changed()

        if d.get('loss_ilcl', True):
            self.rd_loss_ilcl.setChecked(True)
        else:
            self.rd_loss_rc.setChecked(True)

        vi = d.get('verbosity_index', 0)
        if 0 <= vi < self.cmb_info_detail.count():
            self.cmb_info_detail.setCurrentIndex(vi)
        self.chk_text_csv.setChecked(d.get('text_csv', False))

        self.spn_kc_global.setValue(d.get('kc_global', 1.5))
        self.spn_m_global.setValue(d.get('m_global', 0.8))

        lumped = d.get('routing_lumped', True)
        for i, p in enumerate(d.get('isa_params', [])):
            if i >= self.table_areas.rowCount():
                break
            if not lumped:
                w = self.table_areas.cellWidget(i, 1)
                if w: w.setValue(p.get('kc', 1.5))
                w = self.table_areas.cellWidget(i, 2)
                if w: w.setValue(p.get('m', 0.8))
            w = self.table_areas.cellWidget(i, 3)
            if w: w.setValue(p.get('il', 20.0))
            w = self.table_areas.cellWidget(i, 4)
            if w: w.setValue(p.get('cl', 2.5))

        for cmb, key in [(self.cmb_aep_from, 'aep_from'), (self.cmb_aep_to, 'aep_to'),
                         (self.cmb_dur_from, 'dur_from'), (self.cmb_dur_to, 'dur_to')]:
            v = d.get(key, '')
            if v and cmb.findText(v) >= 0:
                cmb.setCurrentText(v)

        self._update_exe_status()

    def _save_setup(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save setup', '', 'RORB setup (*.json)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._collect_setup(), f, indent=2)
        except OSError as e:
            QMessageBox.warning(self, 'Save failed', str(e))

    def _load_setup(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load setup', '', 'RORB setup (*.json)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                d = json.load(f)
            self._apply_setup(d)
        except Exception as e:
            QMessageBox.warning(self, 'Load failed', str(e))

    # ── Run log writer ───────────────────────────────────────────────────────

    def _write_run_log(self, out_dir, result_line):
        """Write a rorb_run_<timestamp>.log into out_dir."""
        if not out_dir:
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            return

        d = self._run_setup or self._collect_setup()
        ts = time.strftime('%Y-%m-%d %H:%M:%S')

        lines = [
            'RORB Run Log',
            '=' * 60,
            f'Run date/time  : {ts}',
            f'RORB_CMD.exe   : {d["exe_path"]}',
            '',
            'Input Files',
            '-' * 40,
            f'Catchment file : {d["catg_path"]}',
        ]

        mode = d['storm_mode']
        if mode == 'stm':
            lines.append(f'Storm file     : {d["stm_path"]}')
        elif mode == 'arr2016':
            lines.append('Storm mode     : ARR2016 ensemble')
            lines.append(f'IFD depths     : {d["ifd_path"]}')
            lines.append(f'Temporal pats  : {d["temporal_path"]}')
            if d.get('hub_path'):
                lines.append(f'ARR Data Hub   : {d["hub_path"]}')
            lines.append(f'AEP range      : {d["aep_from"]} – {d["aep_to"]}')
            lines.append(f'Duration range : {d["dur_from"]} – {d["dur_to"]}')
            if d.get('stm_dir'):
                lines.append(f'.stm folder    : {d["stm_dir"]}')
        elif mode == 'stm_folder':
            lines.append('Storm mode     : Batch .stm folder')
            lines.append(f'.stm folder    : {d["stm_folder"]}')

        lines.append(f'Output folder  : {out_dir}')
        lines += [
            '',
            'Parameters',
            '-' * 40,
            f'Routing        : {"Lumped (single kc/m)" if d["routing_lumped"] else "Non-lumped (per interstation area)"}',
            f'Loss model     : {"Initial loss / Continuing loss" if d["loss_ilcl"] else "Runoff coefficient"}',
        ]
        if d['routing_lumped']:
            lines.append(f'kc             : {d["kc_global"]:.4f}')
            lines.append(f'm              : {d["m_global"]:.4f}')

        lines += ['', 'ISA Parameters', '-' * 40,
                  f'  {"Area":<22}  {"kc":>7}  {"m":>7}  {"IL (mm)":>8}  {"CL (mm/h)":>9}']
        for p in d.get('isa_params', []):
            kc = d['kc_global'] if d['routing_lumped'] else p['kc']
            m  = d['m_global']  if d['routing_lumped'] else p['m']
            lines.append(
                f'  {p["name"]:<22}  {kc:>7.4f}  {m:>7.4f}  {p["il"]:>8.2f}  {p["cl"]:>9.2f}')

        lines += ['', 'Run Result', '-' * 40, result_line]

        log_name = f'rorb_run_{time.strftime("%Y%m%d_%H%M%S")}.log'
        log_path = os.path.join(out_dir, log_name)
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            self._log('info', f'Run log: {log_path}')
        except OSError as e:
            self._log('warn', f'Could not write run log: {e}')

    # ── Run dispatch ──────────────────────────────────────────────────────────

    def _trigger_run(self, mode):
        self._run_mode = mode
        if self._storm_mode == 'arr2016':
            self._on_run_ensemble()
        elif self._storm_mode == 'stm_folder':
            self._on_run_stm_folder()
        else:
            self._on_run_single()

    def _log(self, status, msg):
        icons = {'pass': ('✓', 'color:#1a7a1a;'),
                 'fail': ('✗', 'color:#c0392b;'),
                 'warn': ('⚠', 'color:#d68910;'),
                 'info': ('→', 'color:#2471a3;')}
        icon, css = icons.get(status, ('·', ''))
        self.txt_log.append(f'<span style="{css}"><b>{icon}</b> {msg}</span>')
        QApplication.processEvents()

    # ── Single .stm run ───────────────────────────────────────────────────────

    def _on_run_single(self):
        catg = self.txt_catg.text().strip()
        exe = self.txt_exe.text().strip()
        stm = self.txt_stm.text().strip()

        if not catg or not os.path.isfile(catg):
            QMessageBox.warning(self, 'Missing .catg',
                                'Please select a valid .catg file.')
            return
        if not exe or not os.path.isfile(exe):
            QMessageBox.warning(self, 'RORB_CMD.exe not found',
                                'Please browse to RORB_CMD.exe (installed with RORBwin).')
            return
        if not stm or not os.path.isfile(stm):
            QMessageBox.warning(self, 'Missing .stm',
                                'Please select a valid .stm storm file.')
            return
        if not self._areas:
            QMessageBox.warning(self, 'No interstation areas',
                                'No interstation areas were parsed from the .catg file.')
            return

        areas_params = self._collect_area_params()
        lumped = self.rd_param_single.isChecked()
        verbosity = [3, 2, 1][self.cmb_info_detail.currentIndex()]
        lossmodel = 1 if self.rd_loss_ilcl.isChecked() else 2

        catg_abs = os.path.abspath(catg)
        stm_abs = os.path.abspath(stm)

        catg_for_par = _to_short_path(catg_abs)
        stm_for_par = _to_short_path(stm_abs)

        self._staging_dir = None
        self._staging_inputs = set()
        self._original_output_dir = None

        if not _is_ascii(catg_for_par) or not _is_ascii(stm_for_par):
            stage = tempfile.mkdtemp(prefix='rorb_stage_')
            self._staging_dir = stage
            catg_dst = os.path.join(stage, os.path.basename(catg_abs))
            shutil.copy2(catg_abs, catg_dst)
            self._staging_inputs.add(os.path.basename(catg_abs))
            catg_for_par = catg_dst
            stm_dst = os.path.join(stage, os.path.basename(stm_abs))
            shutil.copy2(stm_abs, stm_dst)
            self._staging_inputs.add(os.path.basename(stm_abs))
            stm_for_par = stm_dst
            self._log('info', f'Non-ASCII path — staging to: {stage}')

        par_fd, par_path = tempfile.mkstemp(suffix='.par')
        os.close(par_fd)
        write_par_file(par_path, catg_for_par, stm_for_par,
                       lumped, verbosity, lossmodel, areas_params)

        self.txt_log.clear()
        self.lbl_status.setText('')
        self.btn_open_results.setEnabled(False)
        self.btn_run_text.setEnabled(False)
        self.btn_run_plot.setEnabled(False)
        self.tabs.setCurrentIndex(4)  # Switch to Log tab
        self._log('info', f'Launching: {par_path}')

        watch_dir = self._staging_dir if self._staging_dir else os.path.dirname(catg_abs)
        if self._staging_dir:
            self._original_output_dir = os.path.dirname(catg_abs)

        self._run_setup = self._collect_setup()
        since_ts = time.time() - 1
        self._worker = _RunWorker(exe, par_path, watch_dir, since_ts)
        self._worker.done.connect(
            lambda ok, text, out_file: self._on_run_done(ok, text, out_file, par_path))
        self._worker.start()

    def _on_run_done(self, ok, text, out_file, par_path):
        try:
            os.unlink(par_path)
        except OSError:
            pass

        if self._staging_dir:
            if ok and self._original_output_dir:
                os.makedirs(self._original_output_dir, exist_ok=True)
                for fname in os.listdir(self._staging_dir):
                    if fname in self._staging_inputs:
                        continue
                    src = os.path.join(self._staging_dir, fname)
                    dst = os.path.join(self._original_output_dir, fname)
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        pass
                if out_file:
                    out_file = os.path.join(self._original_output_dir,
                                            os.path.basename(out_file))
            shutil.rmtree(self._staging_dir, ignore_errors=True)
            self._staging_dir = None
            self._staging_inputs = set()
            self._original_output_dir = None

        # Also copy to explicit output folder if set
        out_dir = self.txt_out_dir.text().strip()
        if ok and out_file and out_dir and os.path.isdir(out_dir):
            dst = os.path.join(out_dir, os.path.basename(out_file))
            try:
                shutil.copy2(out_file, dst)
            except OSError:
                pass

        self.btn_run_text.setEnabled(True)
        self.btn_run_plot.setEnabled(True)

        if text:
            self.txt_log.append(f'<pre>{text}</pre>')

        if ok:
            self._last_out_file = out_file
            # Store peak flow for the Suggest Kc dialog
            if out_file and os.path.isfile(out_file):
                try:
                    from . import arr2016_runner as _arr_r
                    peaks = _arr_r.parse_all_peaks(out_file)
                    if peaks:
                        self._last_peak_m3s = max(v for _, v in peaks)
                except Exception:
                    pass
            self._log('pass', f'Run succeeded → {out_file}')
            self.lbl_status.setText('Run succeeded')
            self.lbl_status.setStyleSheet('color:#1a7a1a;')
            self.btn_open_results.setEnabled(True)
            log_dir = (self.txt_out_dir.text().strip()
                       or (os.path.dirname(out_file) if out_file else ''))
            self._write_run_log(log_dir, f'Result         : Success → {out_file}')
            if self._run_mode == 'plot':
                self._open_in_results()
        else:
            self._last_out_file = None
            self._log('fail', 'Run failed or produced no output — see log above.')
            self.lbl_status.setText('Run failed')
            self.lbl_status.setStyleSheet('color:#c0392b;')
            log_dir = (self.txt_out_dir.text().strip()
                       or os.path.dirname(self.txt_catg.text().strip()))
            self._write_run_log(log_dir, 'Result         : Failed')

    # ── .stm folder batch run ────────────────────────────────────────────────

    def _on_run_stm_folder(self):
        catg = self.txt_catg.text().strip()
        exe = self.txt_exe.text().strip()
        stm_folder = self.txt_stm_input_folder.text().strip()
        out_dir = self.txt_out_dir.text().strip()

        if not exe or not os.path.isfile(exe):
            QMessageBox.warning(self, 'RORB_CMD.exe not found',
                                'Please browse to RORB_CMD.exe.')
            return
        if not catg or not os.path.isfile(catg):
            QMessageBox.warning(self, 'Missing .catg',
                                'Please select a valid .catg file.')
            return
        if not stm_folder or not os.path.isdir(stm_folder):
            QMessageBox.warning(self, 'Missing .stm folder',
                                'Please select a folder containing .stm storm files.')
            return
        if not self._areas:
            QMessageBox.warning(self, 'No interstation areas',
                                'No interstation areas were parsed from the .catg file.')
            return

        if not out_dir:
            out_dir = stm_folder

        areas_params = self._collect_area_params()
        lumped = self.rd_param_single.isChecked()
        verbosity = [3, 2, 1][self.cmb_info_detail.currentIndex()]
        lossmodel = 1 if self.rd_loss_ilcl.isChecked() else 2

        self.txt_log.clear()
        self.lbl_status.setText('')
        self.btn_open_results.setEnabled(False)
        self.btn_run_text.setVisible(False)
        self.btn_run_plot.setVisible(False)
        self.btn_stop.setVisible(True)
        self.btn_stop.setEnabled(True)
        self.tabs.setCurrentIndex(4)

        self._run_setup = self._collect_setup()
        self._last_out_folder = out_dir
        stm_count = sum(1 for f in os.listdir(stm_folder) if f.lower().endswith('.stm'))
        self._log('info', f'Starting batch: {stm_count} .stm file(s) in {stm_folder}')

        self._ens_worker = _StmFolderWorker(
            exe=exe, catg_path=catg,
            stm_folder=stm_folder, out_dir=out_dir,
            lumped=lumped, verbosity=verbosity, lossmodel=lossmodel,
            areas_params=areas_params,
        )
        self._ens_worker.progress.connect(self._on_ens_progress)
        self._ens_worker.done.connect(self._on_ens_done)
        self._ens_worker.start()

    # ── ARR2016 ensemble run ──────────────────────────────────────────────────

    def _on_run_ensemble(self):
        exe = self.txt_exe.text().strip()
        catg = self.txt_catg.text().strip()
        out_dir = self.txt_out_dir.text().strip()
        depths_csv = self.txt_ifd.text().strip()
        rinc_csv = self.txt_temporal.text().strip()
        hub_txt = self.txt_hub.text().strip() or None

        if not exe or not os.path.isfile(exe):
            QMessageBox.warning(self, 'RORB_CMD.exe not found',
                                'Please browse to RORB_CMD.exe.')
            return
        if not catg or not os.path.isfile(catg):
            QMessageBox.warning(self, 'Missing .catg',
                                'Please select a valid .catg file.')
            return
        if not out_dir:
            QMessageBox.warning(self, 'Missing output folder',
                                'Please specify an output folder for ensemble results.')
            return
        if not depths_csv or not os.path.isfile(depths_csv):
            QMessageBox.warning(self, 'Missing IFD depths file',
                                'Please select the IFD design depths .csv file.')
            return
        if not rinc_csv or not os.path.isfile(rinc_csv):
            QMessageBox.warning(self, 'Missing temporal patterns file',
                                'Please select the temporal patterns .csv file.')
            return
        if not self._areas:
            QMessageBox.warning(self, 'No interstation areas',
                                'No interstation areas were parsed from the .catg file.')
            return

        os.makedirs(out_dir, exist_ok=True)

        # Build selected AEP list from range (order from IFD CSV via combo items)
        aep_items = [self.cmb_aep_from.itemText(i) for i in range(self.cmb_aep_from.count())]
        try:
            i_from = aep_items.index(self.cmb_aep_from.currentText())
            i_to = aep_items.index(self.cmb_aep_to.currentText())
        except ValueError:
            i_from, i_to = 0, len(aep_items) - 1
        if i_to < i_from:
            i_from, i_to = i_to, i_from
        selected_aeps = aep_items[i_from:i_to + 1]

        # Build selected duration list from range
        dur_items = [self.cmb_dur_from.itemText(i) for i in range(self.cmb_dur_from.count())]
        try:
            j_from = dur_items.index(self.cmb_dur_from.currentText())
            j_to = dur_items.index(self.cmb_dur_to.currentText())
        except ValueError:
            j_from, j_to = 0, len(dur_items) - 1
        if j_to < j_from:
            j_from, j_to = j_to, j_from
        selected_durs = [self._dur_to_min[d] for d in dur_items[j_from:j_to + 1]
                         if d in self._dur_to_min]

        areas_params = self._collect_area_params()
        lumped = self.rd_param_single.isChecked()
        verbosity = [3, 2, 1][self.cmb_info_detail.currentIndex()]
        lossmodel = 1 if self.rd_loss_ilcl.isChecked() else 2

        self.txt_log.clear()
        self.lbl_status.setText('')
        self.btn_open_results.setEnabled(False)
        self.btn_run_text.setVisible(False)
        self.btn_run_plot.setVisible(False)
        self.btn_stop.setVisible(True)
        self.btn_stop.setEnabled(True)
        self.tabs.setCurrentIndex(4)  # Switch to Log tab

        self._run_setup = self._collect_setup()
        self._last_out_folder = out_dir
        self._log('info', f'Starting ensemble: {len(selected_aeps)} AEPs x '
                  f'{len(selected_durs)} durations')

        stm_dir = self.txt_stm_dir.text().strip() or out_dir

        arf_area_km2 = (self.spn_arf_area.value()
                        if self.chk_arf_area.isChecked() and self.spn_arf_area.value() > 0
                        else None)

        self._ens_worker = _EnsembleWorker(
            exe=exe, catg_path=catg,
            depths_csv=depths_csv, rinc_csv=rinc_csv, hub_txt=hub_txt,
            aep_list=selected_aeps, dur_min_list=selected_durs,
            out_dir=out_dir, stm_dir=stm_dir,
            lumped=lumped, verbosity=verbosity, lossmodel=lossmodel,
            areas_params=areas_params,
            arf_area_km2=arf_area_km2,
        )
        self._ens_worker.progress.connect(self._on_ens_progress)
        self._ens_worker.done.connect(self._on_ens_done)
        self._ens_worker.start()

    def _on_ens_progress(self, line):
        self.txt_log.append(line)
        QApplication.processEvents()

    def _on_stop_ensemble(self):
        if self._ens_worker:
            self._ens_worker.stop()
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText('Stopping…')

    def _on_ens_done(self, ok, summary_path, n_ok, n_total):
        self._ens_summary_path = summary_path
        self.btn_stop.setVisible(False)
        self.btn_stop.setText('Stop')
        self.btn_run_text.setVisible(True)
        self.btn_run_plot.setVisible(False)  # stays hidden in ensemble mode
        result_line = f'Result         : {n_ok}/{n_total} runs succeeded'
        if ok:
            msg = f'Ensemble complete: {n_ok}/{n_total} runs succeeded'
            self.lbl_status.setText(msg)
            self.lbl_status.setStyleSheet('color:#1a7a1a;')
            self.btn_open_results.setEnabled(True)
        else:
            msg = f'Ensemble failed: {n_ok}/{n_total} runs succeeded'
            self.lbl_status.setText(msg)
            self.lbl_status.setStyleSheet('color:#c0392b;')
        self._write_run_log(self._last_out_folder or '', result_line)

    # ── Hand-off to Results Viewer / Explorer ────────────────────────────────

    def _open_in_results(self):
        if self._storm_mode in ('arr2016', 'stm_folder'):
            folder = self._last_out_folder
            if folder and os.path.isdir(folder):
                try:
                    subprocess.Popen(['explorer', os.path.normpath(folder)])
                except Exception:
                    pass
        else:
            if self._last_out_file and self._on_open_results:
                self._on_open_results(os.path.dirname(self._last_out_file))
