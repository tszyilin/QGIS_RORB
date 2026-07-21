# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2026-06-27'
__copyright__ = '(C) 2026 by Tom Norman'

import os
import re

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QWidget,
)
from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QFont

from .compat import ALIGN_CENTER


# ── .catg parsing — helper functions ────────────────────────────────────────

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


# ── .catg parsing — ordered interstation-area list ──────────────────────────

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
        is_print_72 = len(nums) >= 10 and int(nums[9]) == 72

        if not (is_outlet or is_print_72):
            continue

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
        print_code = int(nums[9]) if len(nums) > 9 else 0
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

    from collections import deque
    _unnamed_q70 = deque(
        nid for nid, n in nodes.items()
        if n['print_code'] == 70 and not n['has_text_name']
    )
    _unnamed_q72 = deque(
        nid for nid, n in nodes.items()
        if n['print_code'] == 72 and not n['has_text_name']
    )

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
            raw2 = body.split()[1] if len(body.split()) > 1 else ''
            try:
                _v = float(raw2)
                is_numbered = (_v == float(int(_v)) and int(_v) == int(nums[0]))
            except ValueError:
                is_numbered = False

            if is_numbered:
                if nums[0] != nums[1] or nums[0] != float(int(nums[0])):
                    continue
                rid, from_node, to_node = int(nums[1]), int(nums[2]), int(nums[3])
                length_km = nums[7] if len(nums) > 7 else 0.0
            else:
                if len(nums) < 8 or nums[0] != float(int(nums[0])) or int(nums[0]) < 1:
                    continue
                rid, from_node, to_node = int(nums[0]), int(nums[1]), int(nums[2])
                length_km = nums[6] if len(nums) > 6 else 0.0
        else:
            if nums[0] != float(int(nums[0])) or int(nums[0]) < 1:
                continue
            rid, from_node, to_node = int(nums[0]), int(nums[1]), int(nums[2])
            length_km = nums[6] if len(nums) > 6 else 0.0

        reaches[rid] = {
            'from_node': from_node,
            'to_node': to_node,
            'length_km': length_km,
        }

    node_dn = {}
    for rid, r in reaches.items():
        node_dn[r['from_node']] = (r['to_node'], r['length_km'], rid)

    # ── Control vector ────────────────────────────────────────────────────────
    rows = []
    _hdr_skip = 0
    calc_no = 0
    _cv_basin_idx = 0
    j = 0
    while j < len(lines):
        s = lines[j].strip()
        j += 1
        if s.startswith('C') or not s:
            continue
        if _hdr_skip < 2:
            _hdr_skip += 1
            continue
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
            'reach_len': 0.0,
        }

        comment = parts[3] if len(parts) > 3 else ''

        if code_f in (1.0, 2.0):
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
            m = re.search(r'node\s+(\d+)', comment, re.I)
            if m:
                row['node'] = m.group(1)
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
            while j < len(lines):
                ns = lines[j].strip()
                j += 1
                if ns and not ns.startswith('C'):
                    candidate = ns.split(',')[0].strip()
                    if ',' in ns or candidate == '0':
                        j -= 1
                    else:
                        row['name'] = candidate
                    break
            for nid, n in nodes.items():
                if n['name'] == row['name']:
                    row['node'] = str(nid)
                    break
            if not row['node']:
                q = _unnamed_q72 if code_f > 7.05 else _unnamed_q70
                if q:
                    row['node'] = str(q.popleft())
            row['code_str'] = '7'
            row['_resets_isa'] = code_f > 7.05

        elif code_f == 9.0:
            m = re.search(r'(\d+)', comment)
            if m:
                row['io'] = m.group(1)

        rows.append(row)

    # ── Area assignment ───────────────────────────────────────────────────────
    area_vals = _parse_data_table(lines, 'Areas, km')
    cv_basin_order = [int(r['node']) for r in rows
                      if r['code_str'] in ('1', '2') and r['node']]
    for idx, nid in enumerate(cv_basin_order):
        if idx < len(area_vals) and nid in nodes:
            nodes[nid]['area'] = area_vals[idx]

    # ── Av. Dist. via RORB state-machine simulation ───────────────────────────
    def _wt_combine(av1, a1, av2, a2):
        total = a1 + a2
        if total <= 0:
            return 0.0, 0.0
        return (av1 * a1 + av2 * a2) / total, total

    h_av, h_a = 0.0, 0.0
    sim_stack = []

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
                h_av, h_a = 0.0, 0.0

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


# ── Dialog ───────────────────────────────────────────────────────────────────

class RorbRunDialog(QDialog):

    def __init__(self, iface, parent=None, catg_path=None, stm_path=None,
                 on_open_results=None):
        super().__init__(parent)
        self.iface = iface
        self._on_open_results = on_open_results
        self._calc_order_rows = []

        self._setup_ui()
        self._restore_settings()

        if catg_path:
            self._load_catg(catg_path)

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)

    # ── Settings persistence ─────────────────────────────────────────────────

    def _save_settings(self):
        s = QSettings('RORB', 'RunDialog')
        s.setValue('catg_path', self._calc_txt_catg.text())

    def _restore_settings(self):
        s = QSettings('RORB', 'RunDialog')
        catg = s.value('catg_path', '')
        if catg and os.path.isfile(catg):
            self._load_catg(catg)

    # ── Top-level layout ─────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle('RORB Tools')
        self.setMinimumWidth(700)
        self.setMinimumHeight(520)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        self.tabs = QTabWidget()
        self._add_pipeline_tab()
        self.tabs.addTab(self._build_tab_calc_order(), 'Calc. Order')
        root.addWidget(self.tabs)

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton('Close')
        btn_cancel.clicked.connect(self.close)
        btn_row.addWidget(btn_cancel)
        btn_help = QPushButton('Help')
        btn_help.clicked.connect(self._on_help)
        btn_row.addWidget(btn_help)
        btn_row.addStretch()
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
        self._load_catg(catg_path)
        self.tabs.setCurrentIndex(1)  # Switch to Calc. Order tab

    # ── Tab 1: Calculation Order ─────────────────────────────────────────────

    def _build_tab_calc_order(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

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

        col_labels = ['Name', 'Node', 'Av. Dist. (km)']
        self._calc_table = QTableWidget(0, len(col_labels))
        self._calc_table.setHorizontalHeaderLabels(col_labels)
        self._calc_table.verticalHeader().setVisible(False)
        self._calc_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._calc_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._calc_table.setAlternatingRowColors(True)
        hh = self._calc_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        outer.addWidget(self._calc_table, 1)

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

        return w

    def _populate_calc_order_tab(self, path):
        self._calc_table.setRowCount(0)
        if not path or not os.path.isfile(path):
            return
        self._calc_txt_catg.setText(path)
        try:
            rows, missing = parse_catg_calc_order(path)
        except Exception:
            return
        self._calc_order_rows = rows

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
        pass

    def _browse_calc_catg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB catchment file', '', 'RORB Graphical (*.catg)')
        if path:
            self._load_catg(path)

    # ── .catg loading ─────────────────────────────────────────────────────────

    def _load_catg(self, path):
        self._populate_calc_order_tab(path)

    # ── Help ─────────────────────────────────────────────────────────────────

    def _on_help(self):
        QMessageBox.information(self, 'Help',
                                'Build .catg tab: run the pipeline and build the RORB control file.\n\n'
                                'Calc. Order tab: view the calculation order and average flow distances '
                                'for each print node in the .catg file.')
