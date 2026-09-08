"""
RORB Results Viewer — multi-scenario edition.

Each scenario is a named folder of RORBWin .out files.  All existing tabs
(Files, Critical Events, Hydrograph Viewer, Export) operate on the active
scenario.  A Compare tab (code retained but not added to UI) can overlay
two scenarios' critical hydrographs.

Filename pattern:  {prefix}aep{N}_du{dur}(min|hour)tp{M}.out
"""

import os
import re
import csv as csv_mod
import traceback
from collections import defaultdict

import numpy as np

from qgis.PyQt.QtWidgets import (
    QDialog, QDockWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QTabWidget, QWidget,
    QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox,
    QLineEdit, QCheckBox, QScrollArea,
    QProgressBar, QSplitter, QInputDialog,
    QRadioButton, QButtonGroup, QListWidget, QListWidgetItem,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QFont, QColor

from .core import tef as tef_mod
from .compat import (
    AllDockWidgetAreas, RightDockWidgetArea,
    AlignRightVCenter, AlignCenter,
    Horizontal, Vertical,
    CustomContextMenu,
    NoEditTriggers, SelectRows, HeaderStretch,
    InternalMove, MoveAction,
    UserRole, ItemIsEnabled, ItemIsUserCheckable,
    Checked, Unchecked,
    DialogAccepted,
    HAS_MPL, FigureCanvas, Figure,
)

_TP_COLORS = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]
_SCENARIO_COLORS = ['#2563eb', '#dc2626', '#16a34a', '#9333ea',
                    '#ea580c', '#0891b2']


# ── Filename parser ────────────────────────────────────────────────────────────

def _parse_filename(fname):
    stem = os.path.splitext(fname)[0].lower()
    aep_label = None
    # '1 in N' form: aep1in200, aep1in500, aep1in10000, etc.
    m = re.search(r'aep1in(\d+)', stem)
    if m:
        aep_label = f"1 in {int(m.group(1))}"
    else:
        # Percentage / EY form: aep63_2, aep1, aep2ey, etc.
        m = re.search(r'aep(\d+)(?:[p_](\d+))?(ey)?', stem)
        if m:
            whole = int(m.group(1)); frac = m.group(2); is_ey = bool(m.group(3))
            val = f"{whole}.{frac}" if frac else str(whole)
            aep_label = f"{val} EY" if is_ey else f"{val}% AEP"
    dur_min, dur_label = None, None
    m = re.search(r'du(\d+)(?:_(\d+))?(min|hour)', stem)
    if m:
        whole = int(m.group(1)); frac = m.group(2); unit = m.group(3)
        val = float(f"{whole}.{frac}") if frac else float(whole)
        if unit == 'min':
            dur_min = int(val); dur_label = f"{int(val)} min"
        else:
            dur_min = int(val * 60); dur_label = f"{val:g} hr"
    tp_num = None
    m = re.search(r'tp(\d+)', stem)
    if m: tp_num = int(m.group(1))
    if aep_label and dur_min is not None and tp_num is not None:
        return aep_label, dur_label, dur_min, tp_num
    return None


def _aep_sort_key(aep_label):
    import math
    # '1 in N' → AEP% = 100/N
    m = re.search(r'1 in (\d+)', aep_label)
    if m:
        return -(100.0 / int(m.group(1)))
    m = re.search(r'([\d.]+)', aep_label)
    if not m: return 0.0
    val = float(m.group(1))
    if 'EY' in aep_label:
        val = (1 - math.exp(-val)) * 100  # convert EY → AEP % equivalent
    return -val


# ── Background scanner ─────────────────────────────────────────────────────────

class _ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    result   = pyqtSignal(str, dict)   # scenario_name, files
    error    = pyqtSignal(str, str)    # scenario_name, traceback

    def __init__(self, scenario_name, folder):
        super().__init__()
        self.scenario_name = scenario_name
        self.folder        = folder

    def run(self):
        try:
            from .core.engine import parse_out_hydrograph, parse_out_rainfall, parse_out_ttp
            from concurrent.futures import ThreadPoolExecutor, as_completed

            fnames = sorted(
                f for f in os.listdir(self.folder)
                if f.lower().endswith('.out')
                and not f.lower().startswith('rorb'))

            folder = self.folder

            def _parse_one(fname):
                path   = os.path.join(folder, fname)
                parsed = _parse_filename(fname)
                try:
                    nodes, time_axis, dt, time_shifted = parse_out_hydrograph(path)
                except Exception:
                    nodes, time_axis, dt, time_shifted = {}, [], None, False
                try:
                    ttp_map = parse_out_ttp(path)
                except Exception:
                    ttp_map = {}
                try:
                    rain_t, rain_mm, _ = parse_out_rainfall(path)
                except Exception:
                    rain_t, rain_mm = [], []
                if not rain_mm:
                    stm_path = os.path.splitext(path)[0] + '.stm'
                    if os.path.exists(stm_path):
                        try:
                            from .core.engine import parse_stm
                            dt_stm, rain_stm = parse_stm(stm_path)
                            if dt_stm and rain_stm:
                                n = len(rain_stm)
                                rain_t  = [i * dt_stm for i in range(n + 2)]
                                rain_mm = [0.0] + rain_stm + [0.0]
                        except Exception:
                            pass
                return fname, path, parsed, nodes, time_axis, dt, ttp_map, rain_t, rain_mm, time_shifted

            total = len(fnames); done = 0; out = {}
            with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
                futures = {ex.submit(_parse_one, f): f for f in fnames}
                for fut in futures:
                    fname, path, parsed, nodes, time_axis, dt, ttp_map, rain_t, rain_mm, time_shifted = fut.result()
                    done += 1
                    self.progress.emit(done, total, fname)
                    out[path] = {
                        'fname': fname, 'path': path, 'parsed': parsed,
                        'nodes': nodes, 'time': time_axis, 'dt': dt,
                        'ttp_map': ttp_map,
                        'rain_t': rain_t, 'rain_mm': rain_mm,
                        'time_shifted': time_shifted,
                    }
            self.result.emit(self.scenario_name, out)
        except Exception:
            self.error.emit(self.scenario_name, traceback.format_exc())


# ── Add-scenario dialog ────────────────────────────────────────────────────────

class _AddScenarioDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Scenario")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self._name   = QLineEdit(); self._name.setPlaceholderText("e.g. Base Case")
        self._folder = QLineEdit(); self._folder.setReadOnly(True)
        self._folder.setPlaceholderText("Browse to folder of .out files …")
        browse = QPushButton("Browse…"); browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)
        frow = QHBoxLayout()
        frow.addWidget(self._folder); frow.addWidget(browse)
        form.addRow("Scenario name:", self._name)
        form.addRow("Folder:",        frow)
        lay.addLayout(form)

        btn = QHBoxLayout()
        ok  = QPushButton("OK"); ok.setDefault(True)
        can = QPushButton("Cancel")
        ok.clicked.connect(self.accept); can.clicked.connect(self.reject)
        btn.addStretch(); btn.addWidget(ok); btn.addWidget(can)
        lay.addLayout(btn)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder", "")
        if folder:
            self._folder.setText(folder)
            if not self._name.text():
                self._name.setText(os.path.basename(folder))

    def values(self):
        return self._name.text().strip(), self._folder.text().strip()


# ── Add-multiple-scenarios dialog ─────────────────────────────────────────────

class _AddMultipleScenariosDialog(QDialog):
    """Bulk-add scenarios: one row per (name, folder) pair."""

    def __init__(self, parent=None, existing_names=None,
                 initial_rows=None, title=None, intro=None):
        super().__init__(parent)
        self.setWindowTitle(title or "Add Multiple Scenarios")
        self.setMinimumSize(760, 380)
        self._existing = set(existing_names or [])

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(intro or (
            "Enter one scenario per row. Use 'Add subfolders of…' to bulk-fill "
            "from a parent folder (each subfolder becomes one scenario).")))

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Scenario name", "Folder", ""])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Interactive
                                 if hasattr(hdr, 'ResizeMode') else 0)
        hdr.setStretchLastSection(False)
        try:
            from qgis.PyQt.QtWidgets import QHeaderView as _QH
            hdr.setSectionResizeMode(0, _QH.Interactive)
            hdr.setSectionResizeMode(1, _QH.Stretch)
            hdr.setSectionResizeMode(2, _QH.ResizeToContents)
        except Exception:
            pass
        self._table.setColumnWidth(0, 200)
        lay.addWidget(self._table)

        row_btns = QHBoxLayout()
        add_row  = QPushButton("+ Add row")
        sub_btn  = QPushButton("Add subfolders of…")
        clr_btn  = QPushButton("Clear all")
        add_row.clicked.connect(lambda: self._add_row("", ""))
        sub_btn.clicked.connect(self._add_from_subfolders)
        clr_btn.clicked.connect(lambda: self._table.setRowCount(0))
        row_btns.addWidget(add_row); row_btns.addWidget(sub_btn)
        row_btns.addWidget(clr_btn); row_btns.addStretch()
        lay.addLayout(row_btns)

        btn = QHBoxLayout()
        ok  = QPushButton("Add All"); ok.setDefault(True)
        can = QPushButton("Cancel")
        ok.clicked.connect(self._on_ok); can.clicked.connect(self.reject)
        btn.addStretch(); btn.addWidget(ok); btn.addWidget(can)
        lay.addLayout(btn)

        if initial_rows:
            for name, folder in initial_rows:
                self._add_row(name, folder)
        else:
            for _ in range(3):
                self._add_row("", "")

    def _add_row(self, name, folder):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(name))
        self._table.setItem(row, 1, QTableWidgetItem(folder))
        cell = QWidget(); cl = QHBoxLayout(cell)
        cl.setContentsMargins(2, 0, 2, 0); cl.setSpacing(2)
        b = QPushButton("Browse…"); b.setFixedWidth(70)
        r = QPushButton("✕");       r.setFixedWidth(24)
        b.clicked.connect(lambda _=False, rr=row: self._browse_row(rr))
        r.clicked.connect(lambda _=False, rr=row: self._remove_row(rr))
        cl.addWidget(b); cl.addWidget(r)
        self._table.setCellWidget(row, 2, cell)

    def _browse_row(self, row):
        # Row index passed at connect time can go stale after removals; look up
        # the widget's current row instead.
        w = self.sender().parentWidget()
        for r in range(self._table.rowCount()):
            if self._table.cellWidget(r, 2) is w:
                row = r; break
        folder = QFileDialog.getExistingDirectory(self, "Select folder", "")
        if not folder:
            return
        self._table.setItem(row, 1, QTableWidgetItem(folder))
        name_it = self._table.item(row, 0)
        if name_it is None or not name_it.text().strip():
            self._table.setItem(row, 0, QTableWidgetItem(os.path.basename(folder)))

    def _remove_row(self, row):
        w = self.sender().parentWidget()
        for r in range(self._table.rowCount()):
            if self._table.cellWidget(r, 2) is w:
                self._table.removeRow(r); return

    def _add_from_subfolders(self):
        parent = QFileDialog.getExistingDirectory(
            self, "Select parent folder — each subfolder becomes a scenario", "")
        if not parent:
            return
        try:
            subs = sorted(d for d in os.listdir(parent)
                          if os.path.isdir(os.path.join(parent, d)))
        except OSError as ex:
            QMessageBox.warning(self, "Add subfolders", str(ex)); return
        if not subs:
            QMessageBox.information(self, "Add subfolders",
                                    "No subfolders found."); return
        for d in subs:
            self._add_row(d, os.path.join(parent, d))

    def values(self):
        """Return list of (name, folder) tuples for non-blank rows."""
        out = []
        for r in range(self._table.rowCount()):
            n_it = self._table.item(r, 0); f_it = self._table.item(r, 1)
            name   = n_it.text().strip() if n_it else ""
            folder = f_it.text().strip() if f_it else ""
            if name or folder:
                out.append((name, folder))
        return out

    def _on_ok(self):
        rows = self.values()
        if not rows:
            QMessageBox.warning(self, "Add Multiple",
                                "Add at least one scenario."); return
        errors = []; names_seen = set()
        for i, (name, folder) in enumerate(rows, 1):
            if not name:
                errors.append(f"Row {i}: missing name")
            elif name in self._existing:
                errors.append(f"Row {i}: '{name}' already exists")
            elif name in names_seen:
                errors.append(f"Row {i}: '{name}' duplicated in this list")
            else:
                names_seen.add(name)
            if not folder or not os.path.isdir(folder):
                errors.append(f"Row {i}: folder not valid")
        if errors:
            QMessageBox.warning(self, "Add Multiple",
                                "Fix these first:\n\n" + "\n".join(errors))
            return
        self.accept()


# ── Main dialog ────────────────────────────────────────────────────────────────

class RorbResultsDialog(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("RORB Results Viewer", parent)
        self.setAllowedAreas(AllDockWidgetAreas)
        self._scenarios        = {}   # name -> files dict
        self._scenario_folders = {}   # name -> folder path
        self._active           = None
        self._worker           = None
        self._tp_rows          = {}
        self._crit_rows        = []
        self._env_rows         = []
        self._cmp_scenario_rows = []
        self._set_path  = ''    # path of the loaded/saved scenario-set .json
        self._set_dirty = False
        self._build_ui()
        self._refresh_set_label()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        _container = QWidget()
        _container.setMinimumSize(1150, 800)
        self.setWidget(_container)
        root = QVBoxLayout(_container)

        # ── Scenario-set banner ───────────────────────────────────────────
        self._set_label = QLabel("")
        self._set_label.setMinimumHeight(20)
        root.addWidget(self._set_label)

        # ── Top bar: scan progress + Rep-TP method ────────────────────────
        # (Scenario management moved to the left panel below.)
        # _scen_combo is kept as an invisible source-of-truth that mirrors
        # the scenario list widget, so every existing code path that reads
        # or writes the current scenario keeps working unchanged.
        self._scen_combo = QComboBox()
        self._scen_combo.hide()
        self._scen_combo.currentIndexChanged.connect(self._on_scenario_changed)

        tbar = QHBoxLayout()
        self._scan_progress = QProgressBar(); self._scan_progress.setVisible(False)
        self._scan_progress.setMaximumWidth(200)
        self._scan_status = QLabel("")
        self._scan_status.setStyleSheet("color:gray;font-size:8pt;")
        tbar.addWidget(self._scan_progress)
        tbar.addWidget(self._scan_status)
        tbar.addStretch()
        tbar.addWidget(QLabel("Rep TP:"))
        self._rep_method = QComboBox()
        self._rep_method.addItem("Closest to mean",   "closest")
        self._rep_method.addItem("Closest ≥ mean",    "above")
        self._rep_method.setFixedWidth(150)
        self._rep_method.currentIndexChanged.connect(self._on_rep_method_changed)
        tbar.addWidget(self._rep_method)

        # Scenario-set persistence buttons (top-right)
        tbar.addSpacing(12)
        save_as_btn = QPushButton("Save As…")
        save_btn    = QPushButton("Save")
        import_btn  = QPushButton("Import…")
        save_as_btn.setToolTip("Save the current list of scenarios (name + folder) "
                               "to a new .json file.")
        save_btn.setToolTip("Save the current scenario set to its existing file "
                            "(prompts for a location if no file is loaded).")
        import_btn.setToolTip("Load a previously saved scenario set (.json).")
        save_as_btn.clicked.connect(self._save_scenario_set_as)
        save_btn.clicked.connect(self._save_scenario_set)
        import_btn.clicked.connect(self._load_scenario_set)
        tbar.addWidget(save_as_btn)
        tbar.addWidget(save_btn)
        tbar.addWidget(import_btn)
        root.addLayout(tbar)

        # ── Main splitter: scenarios panel (left) + tabs (right) ──────────
        main_split = QSplitter(Horizontal)

        left = QWidget(); left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(4, 4, 4, 4); left_lay.setSpacing(4)

        hdr = QLabel("Scenarios")
        hdr.setStyleSheet("font-weight:bold; font-size:10pt; padding:2px 0;")
        left_lay.addWidget(hdr)

        self._scen_list = QListWidget()
        self._scen_list.setMinimumWidth(210)
        self._scen_list.setToolTip("Drag scenarios to reorder.")
        self._scen_list.setDragEnabled(True)
        self._scen_list.setAcceptDrops(True)
        self._scen_list.setDropIndicatorShown(True)
        self._scen_list.setDragDropMode(InternalMove)
        self._scen_list.setDefaultDropAction(MoveAction)
        self._scen_list.currentRowChanged.connect(self._on_scen_list_row_changed)
        self._scen_list.itemDoubleClicked.connect(lambda _it: self._rename_scenario())
        self._scen_list.model().rowsMoved.connect(
            lambda *_: self._reorder_scenarios_from_list())
        self._suspend_reorder = False
        left_lay.addWidget(self._scen_list, 1)

        # Add / Remove
        add_row = QHBoxLayout(); add_row.setSpacing(4)
        add_btn  = QPushButton("Add…")
        many_btn = QPushButton("Add Multiple…")
        add_btn.clicked.connect(self._add_scenario)
        many_btn.clicked.connect(self._add_multiple_scenarios)
        add_row.addWidget(add_btn); add_row.addWidget(many_btn)
        left_lay.addLayout(add_row)

        edit_row = QHBoxLayout(); edit_row.setSpacing(4)
        ren_btn = QPushButton("Rename…"); ren_btn.clicked.connect(self._rename_scenario)
        rem_btn = QPushButton("Remove");  rem_btn.clicked.connect(self._remove_scenario)
        clr_btn = QPushButton("Clear");   clr_btn.clicked.connect(self._clear_all_scenarios_prompt)
        edit_row.addWidget(ren_btn); edit_row.addWidget(rem_btn); edit_row.addWidget(clr_btn)
        left_lay.addLayout(edit_row)

        main_split.addWidget(left)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_files(),    "Files")
        self._tabs.addTab(self._tab_critical(), "Critical Events")
        self._tabs.addTab(self._tab_envelope(), "Duration Envelope")
        self._tabs.addTab(self._tab_viewer(),   "Hydrograph Viewer")
        self._tabs.addTab(self._tab_export(),   "Export")
        main_split.addWidget(self._tabs)

        main_split.setStretchFactor(0, 0)
        main_split.setStretchFactor(1, 1)
        main_split.setSizes([230, 920])
        root.addWidget(main_split, 1)

        btn_row = QHBoxLayout(); btn_row.addStretch()
        btn_row.addWidget(QPushButton("Close", clicked=self.close))
        root.addLayout(btn_row)

    # ── Tab 1: Files ─────────────────────────────────────────────────────────

    def _tab_files(self):
        w = QWidget(); lay = QVBoxLayout(w)
        self._file_table = QTableWidget(0, 6)
        self._file_table.setHorizontalHeaderLabels(
            ["Scenario", "Filename", "AEP", "Duration", "TP", "Status"])
        self._file_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._file_table.setEditTriggers(NoEditTriggers)
        self._file_table.setAlternatingRowColors(True)
        self._file_table.setContextMenuPolicy(CustomContextMenu)
        self._file_table.customContextMenuRequested.connect(
            self._file_table_context_menu)
        lay.addWidget(self._file_table)
        return w

    def _refresh_file_table(self):
        self._file_table.setRowCount(0)
        for sname, files in self._scenarios.items():
            for e in sorted(files.values(), key=lambda x: x['fname']):
                row = self._file_table.rowCount()
                self._file_table.insertRow(row)
                si = QTableWidgetItem(sname)
                si.setForeground(QColor(self._scenario_color(sname)))
                self._file_table.setItem(row, 0, si)
                fname_it = QTableWidgetItem(e['fname'])
                fname_it.setData(UserRole, e.get('path', ''))
                self._file_table.setItem(row, 1, fname_it)
                p = e.get('parsed')
                if p:
                    aep, dur_label, _, tp = p
                    self._file_table.setItem(row, 2, QTableWidgetItem(aep))
                    self._file_table.setItem(row, 3, QTableWidgetItem(dur_label))
                    tpi = QTableWidgetItem(str(tp)); tpi.setTextAlignment(AlignCenter)
                    self._file_table.setItem(row, 4, tpi)
                    ok = bool(e.get('nodes'))
                    if ok and e.get('time_shifted'):
                        si2 = QTableWidgetItem("OK  (RORB <6.52 — time corrected)")
                        si2.setForeground(QColor('#d97706'))
                    elif ok:
                        si2 = QTableWidgetItem("OK")
                        si2.setForeground(QColor('#16a34a'))
                    else:
                        si2 = QTableWidgetItem("No hydrograph")
                        si2.setForeground(QColor('#dc2626'))
                    self._file_table.setItem(row, 5, si2)
                else:
                    for c, t in enumerate(["-", "-", "-", "Name not parsed"], 2):
                        it = QTableWidgetItem(t); it.setForeground(QColor('#f97316'))
                        self._file_table.setItem(row, c, it)

    # ── Tab 2: Critical Events ────────────────────────────────────────────────

    def _tab_critical(self):
        w = QWidget(); lay = QVBoxLayout(w)
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel(
            "Critical Events  (ARR 2016: mean of TPs → critical duration → rep TP):"))
        hdr.addStretch()
        hdr.addWidget(QLabel("Node:"))
        self._crit_node_combo = QComboBox(); self._crit_node_combo.setMinimumWidth(160)
        self._crit_node_combo.currentIndexChanged.connect(self._populate_critical_table)
        hdr.addWidget(self._crit_node_combo)
        exp_btn = QPushButton("Export Critical CSV…")
        exp_btn.clicked.connect(self._export_critical_csv)
        hdr.addWidget(exp_btn)
        lay.addLayout(hdr)

        self._crit_table = QTableWidget(0, 7)
        self._crit_table.setHorizontalHeaderLabels([
            "AEP", "Critical Duration", "Rep TP", "# TPs",
            "Mean Peak (m3/s)", "Rep Peak (m3/s)", "Time to Peak (hr)"])
        self._crit_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._crit_table.setEditTriggers(NoEditTriggers)
        self._crit_table.setAlternatingRowColors(True)
        self._crit_table.setSelectionBehavior(SelectRows)
        self._crit_table.setMaximumHeight(220)
        self._crit_table.selectionModel().selectionChanged.connect(
            self._on_crit_row_selected)
        self._crit_table.setContextMenuPolicy(CustomContextMenu)
        self._crit_table.customContextMenuRequested.connect(
            self._crit_table_context_menu)
        lay.addWidget(self._crit_table)

        if HAS_MPL:
            self._crit_fig    = Figure(figsize=(8, 3.5), tight_layout=True)
            self._crit_ax     = self._crit_fig.add_subplot(111)
            self._crit_ax2    = self._crit_ax.twinx()
            self._crit_canvas = FigureCanvas(self._crit_fig)
            lay.addWidget(self._crit_canvas)
        else:
            self._crit_ax = self._crit_ax2 = self._crit_canvas = None
        return w

    def _populate_critical_table(self):
        self._crit_rows = []
        self._crit_table.setRowCount(0)
        node = self._crit_node_combo.currentText() or None
        for aep in self._all_aeps():
            crit = self._compute_critical(aep, node)
            if not crit:
                continue
            rep_e = crit['rep_entry']
            q = self._get_hydro(rep_e, node)
            t = rep_e.get('time', [])[:len(q)] if q is not None else []
            ttp = 0.0
            if q is not None and len(q):
                ttp_map = rep_e.get('ttp_map', {})
                if node and node in ttp_map:
                    ttp = ttp_map[node]
                elif ttp_map:
                    ttp = list(ttp_map.values())[-1]
                else:
                    pk_idx = int(np.argmax(q))
                    ttp = t[pk_idx] if pk_idx < len(t) else 0.0
            crit['ttp'] = ttp; crit['aep'] = aep; crit['node'] = node
            self._crit_rows.append(crit)
            row = self._crit_table.rowCount()
            self._crit_table.insertRow(row)
            self._crit_table.setItem(row, 0, QTableWidgetItem(aep))
            self._crit_table.setItem(row, 1, QTableWidgetItem(crit['crit_dur']))
            tp_it = QTableWidgetItem(str(crit['rep_tp']))
            tp_it.setTextAlignment(AlignCenter)
            self._crit_table.setItem(row, 2, tp_it)
            n_it = QTableWidgetItem(str(crit['n_tps']))
            n_it.setTextAlignment(AlignCenter)
            self._crit_table.setItem(row, 3, n_it)
            for col, val, dec in (
                    (4, crit['mean_peak'], 3),
                    (5, crit['rep_peak'],  3),
                    (6, ttp,               2)):
                it = QTableWidgetItem(f"{val:.{dec}f}")
                it.setTextAlignment(AlignRightVCenter)
                self._crit_table.setItem(row, col, it)

    def _on_crit_row_selected(self):
        if not HAS_MPL: return
        rows = self._crit_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._crit_rows): return
        crit = self._crit_rows[rows[0].row()]
        node = crit.get('node')
        q = self._get_hydro(crit['rep_entry'], node)
        t = crit['rep_entry'].get('time', [])[:len(q)] if q is not None else []
        if q is None: return
        self._crit_ax.clear()
        self._crit_ax2.clear()
        self._crit_ax.plot(t, q, color='steelblue', linewidth=2,
                           label=f"Rep TP{crit['rep_tp']}  ({crit['rep_peak']:.3f} m³/s)")
        self._crit_ax.axhline(crit['mean_peak'], color='#111827', linewidth=1.2,
                              linestyle='--',
                              label=f"Mean  ({crit['mean_peak']:.3f} m³/s)")
        self._crit_ax.set_xlabel("Time (hr)"); self._crit_ax.set_ylabel("Flow (m³/s)")
        self._crit_ax.set_title(
            f"{crit['aep']}  |  Critical: {crit['crit_dur']}  |  "
            f"Rep TP{crit['rep_tp']}  |  {node or 'outlet'}", fontsize=9)
        self._crit_ax.grid(True, alpha=0.25); self._crit_ax.legend(fontsize=8)
        if t:
            pk_t = t[int(np.argmax(q))]
            x_max = min(t[-1], pk_t * 3 + 2) if pk_t > 0 else t[-1]
            self._crit_ax.set_xlim(0, x_max)
        rain_t  = crit['rep_entry'].get('rain_t',  [])
        rain_mm = crit['rep_entry'].get('rain_mm', [])
        if len(rain_t) >= 2 and len(rain_mm) == len(rain_t):
            dt = rain_t[1] - rain_t[0]
            self._crit_ax2.bar(rain_t, rain_mm, width=dt, align='edge',
                               color='#93c5fd', alpha=0.45, zorder=1)
            self._crit_ax2.set_ylabel("Rainfall (mm)", color='#3b82f6', fontsize=8)
            self._crit_ax2.yaxis.set_label_position('right')
            self._crit_ax2.tick_params(axis='y', labelcolor='#3b82f6', labelsize=7)
            max_rain = max(rain_mm) if rain_mm else 1
            self._crit_ax2.set_ylim(max_rain * 4, 0)
        else:
            self._crit_ax2.set_yticks([])
            self._crit_ax2.set_ylabel("")
        self._crit_canvas.draw()

    def _file_table_context_menu(self, pos):
        from qgis.PyQt.QtWidgets import QMenu
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtCore import QUrl

        row = self._file_table.indexAt(pos).row()
        if row < 0:
            return
        fname_it = self._file_table.item(row, 1)
        out_path = fname_it.data(UserRole) if fname_it else ''
        if not out_path:
            return
        stm_path = os.path.splitext(out_path)[0] + '.stm'

        menu = QMenu(self._file_table)
        act_out = menu.addAction(
            f"Open .out  ({os.path.basename(out_path)})" if out_path else "Open .out")
        act_out.setEnabled(bool(out_path) and os.path.exists(out_path))
        act_stm = menu.addAction(
            f"Open .stm  ({os.path.basename(stm_path)})" if stm_path else "Open .stm")
        act_stm.setEnabled(bool(stm_path) and os.path.exists(stm_path))
        menu.addSeparator()
        act_dir = menu.addAction("Open containing folder")
        act_dir.setEnabled(bool(out_path))

        action = menu.exec(self._file_table.viewport().mapToGlobal(pos))
        if action == act_out and os.path.exists(out_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_path))
        elif action == act_stm and os.path.exists(stm_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(stm_path))
        elif action == act_dir and out_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out_path)))

    def _crit_table_context_menu(self, pos):
        from qgis.PyQt.QtWidgets import QMenu
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtCore import QUrl

        rows = self._crit_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._crit_rows):
            return
        crit      = self._crit_rows[rows[0].row()]
        rep_entry = crit.get('rep_entry', {})
        out_path  = rep_entry.get('path', '')
        stm_path  = os.path.splitext(out_path)[0] + '.stm' if out_path else ''

        menu = QMenu(self._crit_table)
        act_out = menu.addAction(
            f"Open .out  ({os.path.basename(out_path)})" if out_path else "Open .out")
        act_out.setEnabled(bool(out_path) and os.path.exists(out_path))

        act_stm = menu.addAction(
            f"Open .stm  ({os.path.basename(stm_path)})" if stm_path else "Open .stm")
        act_stm.setEnabled(bool(stm_path) and os.path.exists(stm_path))

        menu.addSeparator()
        act_dir = menu.addAction("Open containing folder")
        act_dir.setEnabled(bool(out_path))

        action = menu.exec(self._crit_table.viewport().mapToGlobal(pos))
        if action == act_out and os.path.exists(out_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_path))
        elif action == act_stm and os.path.exists(stm_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(stm_path))
        elif action == act_dir and out_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out_path)))

    def _preview_table_context_menu(self, pos):
        from qgis.PyQt.QtWidgets import QMenu
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtCore import QUrl

        row = self._exp_preview_table.indexAt(pos).row()
        if row < 0:
            return
        src_it = self._exp_preview_table.item(row, 0)
        entry  = src_it.data(UserRole) if src_it else None
        if not entry:
            return
        out_path = entry.get('path', '')
        stm_path = os.path.splitext(out_path)[0] + '.stm' if out_path else ''

        menu = QMenu(self._exp_preview_table)
        act_out = menu.addAction(
            f"Open .out  ({os.path.basename(out_path)})" if out_path else "Open .out")
        act_out.setEnabled(bool(out_path) and os.path.exists(out_path))
        act_stm = menu.addAction(
            f"Open .stm  ({os.path.basename(stm_path)})" if stm_path else "Open .stm")
        act_stm.setEnabled(bool(stm_path) and os.path.exists(stm_path))
        menu.addSeparator()
        act_dir = menu.addAction("Open containing folder")
        act_dir.setEnabled(bool(out_path))

        action = menu.exec(self._exp_preview_table.viewport().mapToGlobal(pos))
        if action == act_out and os.path.exists(out_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_path))
        elif action == act_stm and os.path.exists(stm_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(stm_path))
        elif action == act_dir and out_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out_path)))

    def _export_critical_csv(self):
        if not self._crit_rows:
            QMessageBox.warning(self, "Export", "No data."); return
        path, _ = QFileDialog.getSaveFileName(self, "Export Critical Events", "", "CSV (*.csv)")
        if not path: return
        with open(path, 'w', newline='') as f:
            w = csv_mod.writer(f)
            w.writerow(["AEP", "Critical Duration", "Rep TP", "Num TPs",
                        "Mean Peak (m3/s)", "Rep Peak (m3/s)", "Time to Peak (hr)"])
            for r in self._crit_rows:
                w.writerow([r['aep'], r['crit_dur'], r['rep_tp'], r['n_tps'],
                            f"{r['mean_peak']:.4f}", f"{r['rep_peak']:.4f}",
                            f"{r['ttp']:.3f}"])
        QMessageBox.information(self, "Export", f"Exported:\n{path}")

    # ── Tab 3: Duration Envelope  (Q vs duration, box-whisker over all TPs) ───

    def _tab_envelope(self):
        w = QWidget(); root = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("AEP:"))
        self._env_aep_combo = QComboBox(); self._env_aep_combo.setMinimumWidth(130)
        self._env_aep_combo.currentIndexChanged.connect(self._replot_envelope)
        ctrl.addWidget(self._env_aep_combo)
        ctrl.addWidget(QLabel("Node:"))
        self._env_node_combo = QComboBox(); self._env_node_combo.setMinimumWidth(160)
        self._env_node_combo.currentIndexChanged.connect(self._replot_envelope)
        ctrl.addWidget(self._env_node_combo)
        self._env_pts_chk = QCheckBox("TP points"); self._env_pts_chk.setChecked(True)
        self._env_pts_chk.stateChanged.connect(self._replot_envelope)
        ctrl.addWidget(self._env_pts_chk)
        self._env_mean_chk = QCheckBox("Mean line"); self._env_mean_chk.setChecked(True)
        self._env_mean_chk.stateChanged.connect(self._replot_envelope)
        ctrl.addWidget(self._env_mean_chk)
        ctrl.addStretch()
        exp_btn = QPushButton("Export Envelope CSV…")
        exp_btn.clicked.connect(self._export_envelope_csv)
        ctrl.addWidget(exp_btn)
        root.addLayout(ctrl)

        splitter = QSplitter(Vertical)

        if HAS_MPL:
            self._env_fig    = Figure(figsize=(8, 4.2), tight_layout=True)
            self._env_ax     = self._env_fig.add_subplot(111)
            self._env_canvas = FigureCanvas(self._env_fig)
            splitter.addWidget(self._env_canvas)
        else:
            self._env_ax = self._env_canvas = None

        self._env_table = QTableWidget(0, 10)
        self._env_table.setHorizontalHeaderLabels([
            "Duration", "# TPs", "Min", "Q1", "Median", "Mean", "Q3", "Max",
            "Rep TP", "Rep Peak (m³/s)"])
        self._env_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._env_table.setEditTriggers(NoEditTriggers)
        self._env_table.setAlternatingRowColors(True)
        self._env_table.setSelectionBehavior(SelectRows)
        splitter.addWidget(self._env_table)

        splitter.setSizes([460, 220])
        root.addWidget(splitter)
        self._env_rows = []
        return w

    def _refresh_envelope_combos(self):
        aeps  = self._all_aeps()
        nodes = self._all_nodes()
        prev_aep  = self._env_aep_combo.currentText()
        prev_node = self._env_node_combo.currentText()

        self._env_aep_combo.blockSignals(True); self._env_aep_combo.clear()
        for a in aeps: self._env_aep_combo.addItem(a)
        if prev_aep and self._env_aep_combo.findText(prev_aep) >= 0:
            self._env_aep_combo.setCurrentText(prev_aep)
        self._env_aep_combo.blockSignals(False)

        self._env_node_combo.blockSignals(True); self._env_node_combo.clear()
        for n in nodes: self._env_node_combo.addItem(n)
        if prev_node and self._env_node_combo.findText(prev_node) >= 0:
            self._env_node_combo.setCurrentText(prev_node)
        elif nodes:
            self._env_node_combo.setCurrentIndex(len(nodes) - 1)   # outlet
        self._env_node_combo.blockSignals(False)

        self._replot_envelope()

    def _envelope_stats(self, aep, node, scenario=None):
        """Per-duration peak-flow statistics across all temporal patterns."""
        groups = defaultdict(list)
        for e in self._entries_for_aep(aep, scenario):
            _, dur_label, dur_min, tp = e['parsed']
            q  = self._get_hydro(e, node)
            pk = float(np.max(q)) if q is not None and len(q) else 0.0
            groups[(dur_min, dur_label)].append((tp, pk, e))
        rows = []
        for dur_min, dur_label in sorted(groups):
            tps   = sorted(groups[(dur_min, dur_label)], key=lambda x: x[0])
            peaks = np.array([pk for _, pk, _ in tps], dtype=float)
            q1, med, q3 = (float(v) for v in np.percentile(peaks, [25, 50, 75]))
            rep_tp, rep_pk, _rep_e = self._pick_rep(tps)
            rows.append({
                'dur_min': dur_min, 'dur_label': dur_label,
                'tps': tps, 'peaks': peaks,
                'n': len(tps), 'min': float(peaks.min()), 'q1': q1,
                'median': med, 'mean': float(peaks.mean()), 'q3': q3,
                'max': float(peaks.max()),
                'rep_tp': rep_tp, 'rep_peak': rep_pk,
                'critical': False,
            })
        if rows:
            max(rows, key=lambda r: r['mean'])['critical'] = True
        return rows

    def _pick_rep(self, tps):
        """Pick the representative (tp, peak, entry) from [(tp, peak, entry), …]."""
        mean = float(np.mean([pk for _, pk, _ in tps]))
        method = (self._rep_method.currentData()
                  if hasattr(self, '_rep_method') else 'closest')
        if method == 'above':
            above = [x for x in tps if x[1] >= mean]
            if above:
                return min(above, key=lambda x: x[1] - mean)
        return min(tps, key=lambda x: abs(x[1] - mean))

    def _replot_envelope(self):
        if not hasattr(self, '_env_table'):
            return
        aep  = self._env_aep_combo.currentText()
        node = self._env_node_combo.currentText() or None
        rows = self._envelope_stats(aep, node) if aep else []
        self._env_rows = rows

        # ── table ──────────────────────────────────────────────────────────
        self._env_table.setRowCount(0)
        for r in rows:
            row = self._env_table.rowCount()
            self._env_table.insertRow(row)
            dur_it = QTableWidgetItem(
                r['dur_label'] + ("  ★" if r['critical'] else ""))
            if r['critical']:
                f = dur_it.font(); f.setBold(True); dur_it.setFont(f)
                dur_it.setForeground(QColor('#dc2626'))
            self._env_table.setItem(row, 0, dur_it)
            n_it = QTableWidgetItem(str(r['n'])); n_it.setTextAlignment(AlignCenter)
            self._env_table.setItem(row, 1, n_it)
            for col, key in ((2, 'min'), (3, 'q1'), (4, 'median'),
                             (5, 'mean'), (6, 'q3'), (7, 'max')):
                it = QTableWidgetItem(f"{r[key]:.3f}")
                it.setTextAlignment(AlignRightVCenter)
                if key == 'mean' and r['critical']:
                    it.setForeground(QColor('#dc2626'))
                self._env_table.setItem(row, col, it)
            tp_it = QTableWidgetItem(f"TP{r['rep_tp']}")
            tp_it.setTextAlignment(AlignCenter)
            self._env_table.setItem(row, 8, tp_it)
            pk_it = QTableWidgetItem(f"{r['rep_peak']:.3f}")
            pk_it.setTextAlignment(AlignRightVCenter)
            self._env_table.setItem(row, 9, pk_it)

        # ── plot ───────────────────────────────────────────────────────────
        if not HAS_MPL or self._env_ax is None:
            return
        ax = self._env_ax
        ax.clear()
        if not rows:
            ax.set_xlabel("Duration"); ax.set_ylabel("Peak flow (m³/s)")
            self._env_canvas.draw()
            return

        pos  = list(range(1, len(rows) + 1))
        data = [r['peaks'] for r in rows]
        bp = ax.boxplot(data, positions=pos, widths=0.55, patch_artist=True,
                        showmeans=False, whis=(0, 100), zorder=2)
        for i, box in enumerate(bp['boxes']):
            crit = rows[i]['critical']
            box.set_facecolor('#fecaca' if crit else '#dbeafe')
            box.set_edgecolor('#dc2626' if crit else '#2563eb')
            box.set_linewidth(1.6 if crit else 1.0)
        for key in ('whiskers', 'caps'):
            for art in bp[key]:
                art.set_color('#475569'); art.set_linewidth(1.0)
        for med in bp['medians']:
            med.set_color('#111827'); med.set_linewidth(1.6)

        if self._env_pts_chk.isChecked():
            for i, r in enumerate(rows):
                n = r['n']
                offs = (np.linspace(-0.18, 0.18, n) if n > 1 else np.array([0.0]))
                for (tp, pk, _e), off in zip(r['tps'], offs):
                    ax.plot(pos[i] + off, pk, marker='o', markersize=4,
                            color=_TP_COLORS[(tp - 1) % len(_TP_COLORS)],
                            markeredgecolor='#1f2937', markeredgewidth=0.4,
                            linestyle='none', zorder=4)

        if self._env_mean_chk.isChecked():
            means = [r['mean'] for r in rows]
            ax.plot(pos, means, color='#111827', linewidth=1.6, linestyle='--',
                    marker='D', markersize=5, markerfacecolor='#facc15',
                    zorder=5, label="Mean of TPs")

        crit_row = next((i for i, r in enumerate(rows) if r['critical']), None)
        if crit_row is not None:
            r = rows[crit_row]
            ax.axvline(pos[crit_row], color='#dc2626', linewidth=1.0,
                       linestyle=':', alpha=0.6, zorder=1,
                       label=f"Critical: {r['dur_label']}  "
                             f"({r['mean']:.3f} m³/s, TP{r['rep_tp']})")

        ax.set_xticks(pos)
        ax.set_xticklabels([r['dur_label'] for r in rows], rotation=30,
                           ha='right', fontsize=8)
        ax.set_xlim(0.4, len(rows) + 0.6)
        ax.set_xlabel("Storm duration")
        ax.set_ylabel("Peak flow (m³/s)")
        scen = self._active or ''
        prefix = f"{scen}  |  " if scen else ""
        ax.set_title(f"{prefix}{aep}  |  {node or 'outlet'}  |  "
                     f"peak Q vs duration — spread over temporal patterns",
                     fontsize=10)
        ax.grid(True, axis='y', alpha=0.25)
        handles, _lbls = ax.get_legend_handles_labels()
        main_leg = ax.legend(fontsize=8, loc='upper right') if handles else None
        # Separate key for the TP dot colours (same colours as the Hydrograph Viewer).
        # A second ax.legend() call detaches the first, so re-add it afterwards.
        if self._env_pts_chk.isChecked():
            from matplotlib.lines import Line2D
            tp_nums = sorted({tp for r in rows for tp, _pk, _e in r['tps']})
            tp_handles = [
                Line2D([], [], marker='o', linestyle='none', markersize=4,
                       color=_TP_COLORS[(tp - 1) % len(_TP_COLORS)], label=f"TP{tp}")
                for tp in tp_nums]
            if tp_handles:
                ax.legend(handles=tp_handles, fontsize=6.5, loc='lower right',
                          ncol=min(5, len(tp_handles)), handletextpad=0.2,
                          columnspacing=0.7, borderpad=0.4, framealpha=0.85)
                if main_leg is not None:
                    ax.add_artist(main_leg)
        self._env_canvas.draw()

    def _export_envelope_csv(self):
        if not self._env_rows:
            QMessageBox.warning(self, "Export", "No data."); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Duration Envelope", "", "CSV (*.csv)")
        if not path: return
        aep  = self._env_aep_combo.currentText()
        node = self._env_node_combo.currentText() or "outlet"
        max_tps = max(r['n'] for r in self._env_rows)
        with open(path, 'w', newline='') as f:
            w = csv_mod.writer(f)
            w.writerow([f"AEP: {aep}", f"Node: {node}"])
            w.writerow([])
            w.writerow(["Duration", "Num TPs", "Min", "Q1", "Median", "Mean",
                        "Q3", "Max", "Rep TP", "Rep Peak (m3/s)", "Critical"]
                       + ["TP peaks (TP#=m3/s)"] + [""] * (max_tps - 1))
            for r in self._env_rows:
                w.writerow([
                    r['dur_label'], r['n'],
                    f"{r['min']:.4f}", f"{r['q1']:.4f}", f"{r['median']:.4f}",
                    f"{r['mean']:.4f}", f"{r['q3']:.4f}", f"{r['max']:.4f}",
                    f"TP{r['rep_tp']}", f"{r['rep_peak']:.4f}",
                    "Yes" if r['critical'] else "",
                ] + [f"TP{_tp}={pk:.4f}" for _tp, pk, _e in r['tps']])
        QMessageBox.information(self, "Export", f"Exported:\n{path}")

    # ── Tab 4: Hydrograph Viewer ──────────────────────────────────────────────

    def _tab_viewer(self):
        w = QWidget(); root = QVBoxLayout(w)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("AEP:"))
        self._aep_combo = QComboBox(); self._aep_combo.setMinimumWidth(130)
        self._aep_combo.currentIndexChanged.connect(self._refresh_viewer)
        ctrl.addWidget(self._aep_combo)
        ctrl.addWidget(QLabel("Duration:"))
        self._dur_combo = QComboBox(); self._dur_combo.setMinimumWidth(130)
        self._dur_combo.currentIndexChanged.connect(self._on_dur_changed)
        ctrl.addWidget(self._dur_combo)
        ctrl.addWidget(QLabel("Node:"))
        self._node_combo_v = QComboBox(); self._node_combo_v.setMinimumWidth(160)
        self._node_combo_v.currentIndexChanged.connect(self._replot)
        ctrl.addWidget(self._node_combo_v)
        self._mean_chk = QCheckBox("Mean peak line"); self._mean_chk.setChecked(True)
        self._mean_chk.stateChanged.connect(self._replot)
        ctrl.addWidget(self._mean_chk)
        self._hilite_chk = QCheckBox("Highlight rep TP"); self._hilite_chk.setChecked(True)
        self._hilite_chk.stateChanged.connect(self._replot)
        ctrl.addWidget(self._hilite_chk)
        ctrl.addStretch(); root.addLayout(ctrl)

        splitter = QSplitter(Horizontal)
        left = QWidget(); llay = QVBoxLayout(left); llay.setContentsMargins(4,4,4,4)
        llay.addWidget(QLabel("Temporal Patterns:"))
        tbtn = QHBoxLayout()
        all_b = QPushButton("All"); all_b.setFixedWidth(38)
        none_b= QPushButton("None"); none_b.setFixedWidth(44)
        all_b.clicked.connect(lambda: self._toggle_all_tps(True))
        none_b.clicked.connect(lambda: self._toggle_all_tps(False))
        tbtn.addWidget(all_b); tbtn.addWidget(none_b); tbtn.addStretch()
        llay.addLayout(tbtn)
        self._tp_scroll = QScrollArea(); self._tp_scroll.setWidgetResizable(True)
        self._tp_inner = QWidget(); self._tp_vbox = QVBoxLayout(self._tp_inner)
        self._tp_vbox.setSpacing(2); self._tp_vbox.addStretch()
        self._tp_scroll.setWidget(self._tp_inner)
        llay.addWidget(self._tp_scroll)
        self._peak_summary = QLabel("")
        self._peak_summary.setWordWrap(True)
        self._peak_summary.setFont(QFont("Courier New", 8))
        self._peak_summary.setStyleSheet(
            "color:#1e293b;background:#f8fafc;padding:6px;border:1px solid #cbd5e1;")
        llay.addWidget(self._peak_summary)
        splitter.addWidget(left)
        if HAS_MPL:
            self._fig = Figure(figsize=(7,4), tight_layout=True)
            self._ax  = self._fig.add_subplot(111)
            self._ax2 = self._ax.twinx()   # rainfall on inverted right axis
            self._canvas = FigureCanvas(self._fig)
            splitter.addWidget(self._canvas)
        splitter.setSizes([170, 900]); root.addWidget(splitter)
        return w

    # ── Tab 4: Compare (not shown in tabs — code retained for re-enabling) ────

    def _tab_compare(self):
        w = QWidget(); root = QVBoxLayout(w)

        scen_box = QGroupBox("Scenarios to Compare")
        scen_lay = QVBoxLayout(scen_box)
        self._cmp_scen_container = QWidget()
        self._cmp_scen_vbox      = QVBoxLayout(self._cmp_scen_container)
        self._cmp_scen_vbox.setSpacing(3); self._cmp_scen_vbox.setContentsMargins(0,0,0,0)
        scen_lay.addWidget(self._cmp_scen_container)
        add_row_btn = QPushButton("+ Add Scenario"); add_row_btn.setFixedWidth(120)
        add_row_btn.clicked.connect(self._cmp_add_row)
        scen_lay.addWidget(add_row_btn)
        root.addWidget(scen_box)
        self._cmp_scenario_rows = []

        metric_box = QGroupBox("Compare by")
        metric_lay = QHBoxLayout(metric_box)
        self._cmp_metric = QButtonGroup()
        for i, (lbl, key) in enumerate([
                ("Peak flow (m³/s)",    "peak"),
                ("Critical duration",   "crit_dur"),
                ("Time to peak (hr)",   "ttp")]):
            rb = QRadioButton(lbl)
            if i == 0: rb.setChecked(True)
            self._cmp_metric.addButton(rb, i)
            metric_lay.addWidget(rb)
        metric_lay.addStretch()
        root.addWidget(metric_box)

        ctrl = QHBoxLayout()
        run_btn = QPushButton("Compare"); run_btn.setMinimumHeight(30)
        run_btn.setFixedWidth(100)
        run_btn.clicked.connect(self._run_compare)
        ctrl.addWidget(run_btn); ctrl.addStretch()
        root.addLayout(ctrl)

        self._cmp_table = QTableWidget(0, 1)
        self._cmp_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._cmp_table.setEditTriggers(NoEditTriggers)
        self._cmp_table.setAlternatingRowColors(True)
        self._cmp_table.setSelectionBehavior(SelectRows)
        self._cmp_table.setMaximumHeight(220)
        self._cmp_table.selectionModel().selectionChanged.connect(
            self._on_cmp_row_selected)
        root.addWidget(self._cmp_table)

        if HAS_MPL:
            self._cmp_fig    = Figure(figsize=(8, 3.5), tight_layout=True)
            self._cmp_ax     = self._cmp_fig.add_subplot(111)
            self._cmp_canvas = FigureCanvas(self._cmp_fig)
            root.addWidget(self._cmp_canvas)
        else:
            self._cmp_ax = self._cmp_canvas = None

        self._cmp_data_rows = []
        return w

    # ── Compare helpers ───────────────────────────────────────────────────────

    def _cmp_add_row(self, scen_name=None):
        if not hasattr(self, '_cmp_scen_vbox'):
            return
        row_w = QWidget(); row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(0, 1, 0, 1); row_l.setSpacing(6)

        scen_combo = QComboBox(); scen_combo.setMinimumWidth(180)
        node_combo = QComboBox(); node_combo.setMinimumWidth(150)

        for name in self._scenarios.keys():
            scen_combo.addItem(name)
        if scen_name and scen_name in self._scenarios:
            scen_combo.setCurrentText(scen_name)
        elif self._cmp_scenario_rows:
            used = {rd['scen'].currentText() for rd in self._cmp_scenario_rows}
            for name in self._scenarios:
                if name not in used:
                    scen_combo.setCurrentText(name); break

        scen_combo.currentIndexChanged.connect(
            lambda: self._cmp_refresh_node(scen_combo, node_combo))
        self._cmp_refresh_node(scen_combo, node_combo)

        rem_btn = QPushButton("✕"); rem_btn.setFixedSize(22, 22)

        row_l.addWidget(QLabel("Scenario:")); row_l.addWidget(scen_combo)
        row_l.addWidget(QLabel("Node:"));     row_l.addWidget(node_combo)
        row_l.addWidget(rem_btn); row_l.addStretch()

        rd = {'scen': scen_combo, 'node': node_combo, 'w': row_w}
        self._cmp_scenario_rows.append(rd)
        self._cmp_scen_vbox.addWidget(row_w)
        rem_btn.clicked.connect(lambda: self._cmp_remove_row(rd))

    def _cmp_remove_row(self, rd):
        if len(self._cmp_scenario_rows) <= 2:
            QMessageBox.information(self, "Compare", "Need at least 2 scenarios.")
            return
        self._cmp_scenario_rows.remove(rd)
        rd['w'].deleteLater()

    def _cmp_refresh_node(self, scen_combo, node_combo):
        name  = scen_combo.currentText()
        nodes = self._all_nodes(name)
        node_combo.blockSignals(True); node_combo.clear()
        for n in nodes: node_combo.addItem(n)
        node_combo.blockSignals(False)

    def _run_compare(self):
        if len(self._cmp_scenario_rows) < 2:
            QMessageBox.warning(self, "Compare", "Add at least two scenarios."); return

        names = [rd['scen'].currentText() for rd in self._cmp_scenario_rows]
        nodes = [rd['node'].currentText() or None for rd in self._cmp_scenario_rows]

        aep_sets = [set(self._all_aeps(n)) for n in names]
        common_aeps = sorted(aep_sets[0].intersection(*aep_sets[1:]), key=_aep_sort_key)

        metric_id = self._cmp_metric.checkedId()

        self._cmp_data_rows = []
        for aep in common_aeps:
            crits = [self._compute_critical(aep, nodes[i], names[i])
                     for i in range(len(names))]
            if any(c is None for c in crits): continue
            self._cmp_data_rows.append((aep, crits, nodes, names))

        if metric_id == 0:
            scen_cols = []
            for i, name in enumerate(names):
                scen_cols += [f"{name}\nCrit Dur", f"{name}\nPeak (m³/s)"]
            headers = ["AEP"] + scen_cols + ["Range (m³/s)", "Range (%)"]
        elif metric_id == 1:
            headers = ["AEP"] + [f"{n}\nCrit Dur" for n in names] + ["Changed?"]
        else:
            headers = ["AEP"] + [f"{n}\nTTP (hr)" for n in names] + ["Range (hr)"]

        self._cmp_table.setColumnCount(len(headers))
        self._cmp_table.setHorizontalHeaderLabels(headers)
        self._cmp_table.setRowCount(0)

        for aep, crits, nodes_, names_ in self._cmp_data_rows:
            row = self._cmp_table.rowCount()
            self._cmp_table.insertRow(row)
            self._cmp_table.setItem(row, 0, QTableWidgetItem(aep))

            if metric_id == 0:
                peaks = [c['rep_peak'] for c in crits]
                col = 1
                for i, c in enumerate(crits):
                    self._cmp_table.setItem(row, col, QTableWidgetItem(c['crit_dur']))
                    pk_it = QTableWidgetItem(f"{peaks[i]:.3f}")
                    pk_it.setTextAlignment(AlignRightVCenter)
                    pk_it.setForeground(QColor(_SCENARIO_COLORS[i % len(_SCENARIO_COLORS)]))
                    self._cmp_table.setItem(row, col + 1, pk_it)
                    col += 2
                rng = max(peaks) - min(peaks)
                pct = (rng / min(peaks) * 100) if min(peaks) else 0
                r_it = QTableWidgetItem(f"{rng:.3f}")
                r_it.setTextAlignment(AlignRightVCenter)
                r_it.setForeground(QColor('#dc2626' if rng > 0 else '#6b7280'))
                self._cmp_table.setItem(row, col, r_it)
                p_it = QTableWidgetItem(f"{pct:.1f}%")
                p_it.setTextAlignment(AlignRightVCenter)
                self._cmp_table.setItem(row, col + 1, p_it)

            elif metric_id == 1:
                durs  = [c['crit_dur'] for c in crits]
                changed = len(set(durs)) > 1
                for i, d in enumerate(durs):
                    it = QTableWidgetItem(d)
                    if changed: it.setForeground(QColor('#ea580c'))
                    self._cmp_table.setItem(row, i + 1, it)
                cc_it = QTableWidgetItem("Yes ⚠" if changed else "—")
                cc_it.setTextAlignment(AlignCenter)
                if changed: cc_it.setForeground(QColor('#ea580c'))
                self._cmp_table.setItem(row, len(crits) + 1, cc_it)

            else:
                ttps = []
                for i, c in enumerate(crits):
                    q = self._get_hydro(c['rep_entry'], nodes_[i])
                    t = c['rep_entry'].get('time', [])
                    ttp = 0.0
                    if q is not None and len(q):
                        pk_idx = int(np.argmax(q))
                        ttp = t[pk_idx] if pk_idx < len(t) else 0.0
                    ttps.append(ttp)
                    ttp_it = QTableWidgetItem(f"{ttp:.2f}")
                    ttp_it.setTextAlignment(AlignRightVCenter)
                    ttp_it.setForeground(
                        QColor(_SCENARIO_COLORS[i % len(_SCENARIO_COLORS)]))
                    self._cmp_table.setItem(row, i + 1, ttp_it)
                rng = max(ttps) - min(ttps) if ttps else 0
                r_it = QTableWidgetItem(f"{rng:.2f}")
                r_it.setTextAlignment(AlignRightVCenter)
                self._cmp_table.setItem(row, len(crits) + 1, r_it)

    def _on_cmp_row_selected(self):
        if not HAS_MPL or not self._cmp_data_rows: return
        rows = self._cmp_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._cmp_data_rows): return
        aep, crits, nodes, names = self._cmp_data_rows[rows[0].row()]

        self._cmp_ax.clear()
        linestyles = ['-', '--', ':', '-.']
        for i, (crit, node, name) in enumerate(zip(crits, nodes, names)):
            q = self._get_hydro(crit['rep_entry'], node)
            t = crit['rep_entry'].get('time', [])[:len(q)] if q is not None else []
            if q is None: continue
            col = _SCENARIO_COLORS[i % len(_SCENARIO_COLORS)]
            ls  = linestyles[i % len(linestyles)]
            self._cmp_ax.plot(t, q, color=col, linewidth=2, linestyle=ls,
                              label=f"{name}  TP{crit['rep_tp']}  "
                                    f"({crit['rep_peak']:.3f} m³/s)")

        self._cmp_ax.set_xlabel("Time (hr)"); self._cmp_ax.set_ylabel("Flow (m³/s)")
        self._cmp_ax.set_title(f"{aep}  |  Scenario comparison", fontsize=10)
        self._cmp_ax.grid(True, alpha=0.25)
        self._cmp_ax.legend(fontsize=8)
        self._cmp_canvas.draw()

    # ── Tab 5: Export ─────────────────────────────────────────────────────────

    def _tab_export(self):
        w = QWidget(); lay = QVBoxLayout(w)

        box = QGroupBox("Export Settings"); form = QFormLayout(box)
        # Kept as an invisible source-of-truth so the rest of the export code
        # (via _exp_scenario()) still works unchanged.
        self._exp_scen_combo = QComboBox(); self._exp_scen_combo.hide()
        self._exp_scen_lbl = QLabel("(no scenario loaded)")
        self._exp_scen_lbl.setStyleSheet(
            "color:#166534; font-weight:bold; padding:2px 6px;")
        self._exp_scen_lbl.setToolTip(
            "Follows the scenario selected in the left panel — "
            "switch there to export from a different scenario.")
        form.addRow("Scenario (follows left panel):", self._exp_scen_lbl)
        self._exp_node_combo = QComboBox(); self._exp_node_combo.setMinimumWidth(240)
        self._exp_node_combo.currentIndexChanged.connect(self._on_exp_node_changed)
        form.addRow("Node (print point):", self._exp_node_combo)
        folder_row = QHBoxLayout()
        self._exp_folder_edit = QLineEdit(); self._exp_folder_edit.setReadOnly(True)
        self._exp_folder_edit.setPlaceholderText("Browse to output folder …")
        folder_btn = QPushButton("Browse…"); folder_btn.setFixedWidth(80)
        folder_btn.clicked.connect(self._browse_export_folder)
        folder_row.addWidget(self._exp_folder_edit); folder_row.addWidget(folder_btn)
        form.addRow("Save folder:", folder_row)

        # ── Naming (optional) — TUFLOW .tef labels + filename template ────
        self._tef_maps = None
        self._tef_path = None
        self._name_template = ''
        tef_row = QHBoxLayout()
        tef_btn = QPushButton("Import .tef…"); tef_btn.setFixedWidth(110)
        tef_btn.clicked.connect(self._browse_tef)
        tef_row.addWidget(tef_btn)
        self._tef_lbl = QLabel("(none loaded)")
        self._tef_lbl.setStyleSheet("color:#6b7280; font-style:italic;")
        tef_row.addWidget(self._tef_lbl); tef_row.addStretch()
        clr_btn = QPushButton("Clear .tef"); clr_btn.setFixedWidth(90)
        clr_btn.clicked.connect(self._clear_tef)
        tef_row.addWidget(clr_btn)
        form.addRow(".tef event file:", tef_row)

        tmpl_row = QHBoxLayout()
        self._name_template_edit = QLineEdit()
        self._name_template_edit.setPlaceholderText(
            "e.g. ~AEP~_~DUR~_~TP~   — leave blank to keep original filenames")
        self._name_template_edit.textChanged.connect(self._on_name_template_changed)
        tmpl_row.addWidget(self._name_template_edit)
        reset_btn = QPushButton("~AEP~_~DUR~_~TP~"); reset_btn.setFixedWidth(150)
        reset_btn.setToolTip("Insert the default template")
        reset_btn.clicked.connect(
            lambda: self._name_template_edit.setText('~AEP~_~DUR~_~TP~'))
        tmpl_row.addWidget(reset_btn)
        form.addRow("Filename template:", tmpl_row)
        form.addRow("", QLabel(
            "<i>Tokens <b>~AEP~ ~DUR~ ~TP~</b> get replaced by labels from the "
            ".tef (or sensible defaults when no .tef is loaded). "
            "Reorder by editing the template — e.g. <b>~DUR~_~AEP~_~TP~</b>.</i>"))
        lay.addWidget(box)

        splitter = QSplitter(Vertical)

        # ── Selected Critical Events ───────────────────────────────────────
        crit_w = QWidget(); crit_lay = QVBoxLayout(crit_w)
        crit_lay.setContentsMargins(0, 4, 0, 0)
        crit_hdr = QHBoxLayout()
        crit_hdr.addWidget(QLabel("<b>Selected Critical Events</b>"))
        crit_hdr.addStretch()
        all_c  = QPushButton("All");  all_c.setFixedWidth(38)
        none_c = QPushButton("None"); none_c.setFixedWidth(45)
        all_c.clicked.connect(lambda: self._toggle_crit_events(True))
        none_c.clicked.connect(lambda: self._toggle_crit_events(False))
        crit_hdr.addWidget(all_c); crit_hdr.addWidget(none_c)
        crit_lay.addLayout(crit_hdr)
        self._exp_crit_table = QTableWidget(0, 4)
        self._exp_crit_table.setHorizontalHeaderLabels(
            ["AEP", "Critical Duration", "Rep TP", "Rep Peak (m³/s)"])
        self._exp_crit_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._exp_crit_table.setEditTriggers(NoEditTriggers)
        self._exp_crit_table.setAlternatingRowColors(True)
        self._exp_crit_table.itemChanged.connect(self._refresh_preview)
        crit_lay.addWidget(self._exp_crit_table)
        splitter.addWidget(crit_w)

        # ── Extra Events ──────────────────────────────────────────────────
        extra_w = QWidget(); extra_lay = QVBoxLayout(extra_w)
        extra_lay.setContentsMargins(0, 4, 0, 0)
        extra_lay.addWidget(QLabel("<b>Extra Events</b>"))
        pick = QHBoxLayout()
        pick.addWidget(QLabel("AEP:"))
        self._extra_aep_combo = QComboBox(); self._extra_aep_combo.setMinimumWidth(120)
        self._extra_aep_combo.currentIndexChanged.connect(self._refresh_extra_dur)
        pick.addWidget(self._extra_aep_combo)
        pick.addWidget(QLabel("Duration:"))
        self._extra_dur_combo = QComboBox(); self._extra_dur_combo.setMinimumWidth(100)
        self._extra_dur_combo.currentIndexChanged.connect(self._refresh_extra_tp)
        pick.addWidget(self._extra_dur_combo)
        pick.addWidget(QLabel("TP:"))
        self._extra_tp_combo = QComboBox(); self._extra_tp_combo.setMinimumWidth(80)
        pick.addWidget(self._extra_tp_combo)
        add_btn = QPushButton("Add"); add_btn.setFixedWidth(50)
        add_btn.clicked.connect(self._add_extra_event)
        pick.addWidget(add_btn); pick.addStretch()
        extra_lay.addLayout(pick)
        self._exp_extra_table = QTableWidget(0, 3)
        self._exp_extra_table.setHorizontalHeaderLabels(["AEP", "Duration", "TP"])
        self._exp_extra_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._exp_extra_table.setEditTriggers(NoEditTriggers)
        self._exp_extra_table.setAlternatingRowColors(True)
        self._exp_extra_table.setSelectionBehavior(SelectRows)
        self._exp_extra_table.setContextMenuPolicy(CustomContextMenu)
        self._exp_extra_table.customContextMenuRequested.connect(
            self._extra_table_context_menu)
        rem_btn = QPushButton("Remove selected"); rem_btn.setFixedWidth(130)
        rem_btn.clicked.connect(self._remove_extra_event)
        rem_row = QHBoxLayout(); rem_row.addWidget(rem_btn); rem_row.addStretch()
        extra_lay.addWidget(self._exp_extra_table)
        extra_lay.addLayout(rem_row)
        splitter.addWidget(extra_w)
        self._exp_extra_rows = []

        # ── Preview ───────────────────────────────────────────────────────
        prev_w = QWidget(); prev_lay = QVBoxLayout(prev_w)
        prev_lay.setContentsMargins(0, 4, 0, 0)
        prev_lay.addWidget(QLabel("<b>Preview</b>  — files to export  "
                                  "( _hydro.csv  /  _rf.csv )"))
        self._custom_stems = {}
        self._exp_preview_table = QTableWidget(0, 5)
        self._exp_preview_table.setHorizontalHeaderLabels(
            ["Source", "AEP", "Duration", "TP", "Export name  (editable)"])
        self._exp_preview_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._exp_preview_table.setAlternatingRowColors(True)
        self._exp_preview_table.itemChanged.connect(self._on_preview_name_changed)
        self._exp_preview_table.setContextMenuPolicy(CustomContextMenu)
        self._exp_preview_table.customContextMenuRequested.connect(
            self._preview_table_context_menu)
        prev_lay.addWidget(self._exp_preview_table)
        self._exp_version_warn = QLabel("")
        self._exp_version_warn.setWordWrap(True)
        self._exp_version_warn.setStyleSheet(
            "color:#92400e;background:#fef3c7;padding:4px 6px;"
            "border:1px solid #fcd34d;border-radius:3px;")
        self._exp_version_warn.setVisible(False)
        prev_lay.addWidget(self._exp_version_warn)
        splitter.addWidget(prev_w)

        splitter.setSizes([180, 130, 160])
        lay.addWidget(splitter)

        brow = QHBoxLayout()
        hydro_btn = QPushButton("Export Hydrographs  (_hydro.csv)")
        hydro_btn.setMinimumHeight(34); hydro_btn.clicked.connect(self._export_hydros)
        hyeto_btn = QPushButton("Export Hyetographs  (_rf.csv)")
        hyeto_btn.setMinimumHeight(34); hyeto_btn.clicked.connect(self._export_hyetos)
        tp_btn = QPushButton("Export Temporal Patterns  (_tp.csv)")
        tp_btn.setMinimumHeight(34); tp_btn.clicked.connect(self._export_temporal)
        xl_btn = QPushButton("Export Excel  (.xlsx — one sheet per case)")
        xl_btn.setMinimumHeight(34); xl_btn.clicked.connect(self._export_excel)
        brow.addWidget(hydro_btn); brow.addWidget(hyeto_btn); brow.addWidget(tp_btn)
        brow.addWidget(xl_btn)
        brow.addStretch()
        lay.addLayout(brow)
        return w

    # ── Export helpers ────────────────────────────────────────────────────────

    def _browse_export_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder", "")
        if folder:
            self._exp_folder_edit.setText(folder)

    def _exp_scenario(self):
        return self._exp_scen_combo.currentText() or self._active or None

    def _on_exp_node_changed(self):
        self._refresh_exp_crit_table()
        self._refresh_preview()

    def _toggle_crit_events(self, state):
        self._exp_crit_table.blockSignals(True)
        for row in range(self._exp_crit_table.rowCount()):
            it = self._exp_crit_table.item(row, 0)
            if it: it.setCheckState(Checked if state else Unchecked)
        self._exp_crit_table.blockSignals(False)
        self._refresh_preview()

    def _refresh_exp_crit_table(self):
        self._exp_crit_table.blockSignals(True)
        self._exp_crit_table.setRowCount(0)
        scen = self._exp_scenario()
        node = self._exp_node_combo.currentText() or None
        for aep in self._all_aeps(scen):
            crit = self._compute_critical(aep, node, scen)
            if not crit: continue
            row = self._exp_crit_table.rowCount()
            self._exp_crit_table.insertRow(row)
            aep_it = QTableWidgetItem(aep)
            aep_it.setFlags(ItemIsEnabled | ItemIsUserCheckable)
            aep_it.setCheckState(Checked)
            aep_it.setData(UserRole, {'aep': aep, **crit})
            self._exp_crit_table.setItem(row, 0, aep_it)
            self._exp_crit_table.setItem(row, 1, QTableWidgetItem(crit['crit_dur']))
            tp_it = QTableWidgetItem(f"TP{crit['rep_tp']}")
            tp_it.setTextAlignment(AlignCenter)
            self._exp_crit_table.setItem(row, 2, tp_it)
            pk_it = QTableWidgetItem(f"{crit['rep_peak']:.3f}")
            pk_it.setTextAlignment(AlignRightVCenter)
            self._exp_crit_table.setItem(row, 3, pk_it)
        self._exp_crit_table.blockSignals(False)

    def _refresh_extra_aep(self):
        scen = self._exp_scenario()
        aeps = self._all_aeps(scen)
        self._extra_aep_combo.blockSignals(True); self._extra_aep_combo.clear()
        for a in aeps: self._extra_aep_combo.addItem(a)
        self._extra_aep_combo.blockSignals(False)
        self._refresh_extra_dur()

    def _refresh_extra_dur(self):
        scen = self._exp_scenario()
        aep  = self._extra_aep_combo.currentText()
        entries = self._entries_for_aep(aep, scen) if aep else []
        durs = sorted(set((e['parsed'][1], e['parsed'][2]) for e in entries),
                      key=lambda x: x[1])
        self._extra_dur_combo.blockSignals(True); self._extra_dur_combo.clear()
        for lbl, mins in durs: self._extra_dur_combo.addItem(lbl, mins)
        self._extra_dur_combo.blockSignals(False)
        self._refresh_extra_tp()

    def _refresh_extra_tp(self):
        scen    = self._exp_scenario()
        aep     = self._extra_aep_combo.currentText()
        dur_min = self._extra_dur_combo.currentData()
        entries = self._entries_for_aep(aep, scen) if aep else []
        if dur_min is not None:
            entries = [e for e in entries if e['parsed'][2] == dur_min]
        node = self._exp_node_combo.currentText() or None
        crit = self._compute_critical(aep, node, scen) if aep else None
        rep_tp = crit['rep_tp'] if crit else None
        self._extra_tp_combo.blockSignals(True); self._extra_tp_combo.clear()
        for e in sorted(entries, key=lambda x: x['parsed'][3]):
            tp_num = e['parsed'][3]
            label  = f"TP{tp_num}" + (" ★" if tp_num == rep_tp else "")
            self._extra_tp_combo.addItem(label, tp_num)
        self._extra_tp_combo.blockSignals(False)

    def _add_extra_event(self):
        scen    = self._exp_scenario()
        aep     = self._extra_aep_combo.currentText()
        dur_min = self._extra_dur_combo.currentData()
        tp_num  = self._extra_tp_combo.currentData()
        dur_lbl = self._extra_dur_combo.currentText()
        if not aep or dur_min is None or tp_num is None: return
        entry = next(
            (e for e in self._entries_for_aep(aep, scen)
             if e['parsed'][2] == dur_min and e['parsed'][3] == tp_num),
            None)
        if entry is None:
            QMessageBox.warning(self, "Extra Events", "No matching file found."); return
        for row_data in self._exp_extra_rows:
            if row_data[:4] == (aep, dur_lbl, dur_min, tp_num): return
        self._exp_extra_rows.append((aep, dur_lbl, dur_min, tp_num, entry))
        row = self._exp_extra_table.rowCount()
        self._exp_extra_table.insertRow(row)
        aep_it = QTableWidgetItem(aep)
        aep_it.setData(UserRole, entry.get('path', ''))
        self._exp_extra_table.setItem(row, 0, aep_it)
        self._exp_extra_table.setItem(row, 1, QTableWidgetItem(dur_lbl))
        tp_it = QTableWidgetItem(f"TP{tp_num}"); tp_it.setTextAlignment(AlignCenter)
        self._exp_extra_table.setItem(row, 2, tp_it)
        self._refresh_preview()

    def _remove_extra_event(self):
        rows = sorted(
            set(i.row() for i in self._exp_extra_table.selectedItems()),
            reverse=True)
        for r in rows:
            self._exp_extra_table.removeRow(r)
            if r < len(self._exp_extra_rows):
                self._exp_extra_rows.pop(r)
        self._refresh_preview()

    def _get_export_events(self):
        """Return list of (source, aep, dur_label, tp_num, entry)."""
        events = []
        for row in range(self._exp_crit_table.rowCount()):
            it = self._exp_crit_table.item(row, 0)
            if not it or it.checkState() != Checked: continue
            data = it.data(UserRole)
            if not data: continue
            events.append(("Critical ★", data['aep'], data['crit_dur'],
                           data['rep_tp'], data['rep_entry']))
        for aep, dur_label, dur_min, tp_num, entry in self._exp_extra_rows:
            events.append(("Extra", aep, dur_label, tp_num, entry))
        return events

    def _entry_stem(self, entry):
        raw = os.path.splitext(entry['fname'])[0]
        m   = re.search(r'aep\d', raw, re.IGNORECASE)
        return raw[m.start():] if m else raw

    def _templated_stem(self, aep, dur_min, tp_num):
        """Filename stem from _name_template + _tef_maps, or '' if no template."""
        if not self._name_template:
            return ''
        return tef_mod.apply_template(
            self._name_template, aep, dur_min, tp_num,
            self._tef_maps or {})

    def _browse_tef(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import TUFLOW Event File",
            self._tef_path or "", "TUFLOW Event File (*.tef);;All files (*)")
        if not path:
            return
        if self._apply_tef_path(path, show_errors=True):
            self._mark_set_dirty()

    def _apply_tef_path(self, path, show_errors=False):
        """Load a .tef into _tef_maps and update the label. Returns True on success."""
        try:
            maps = tef_mod.parse_tef(path)
        except Exception as e:
            if show_errors:
                QMessageBox.warning(self, ".tef", f"Could not read:\n{e}")
            return False
        n = sum(len(v) for v in maps.values())
        if n == 0:
            if show_errors:
                QMessageBox.warning(
                    self, ".tef",
                    "No ~DUR~ / ~AEP~ / ~TP~ event blocks found in that file.")
            return False
        self._tef_path = path
        self._tef_maps = maps
        self._tef_lbl.setText(
            f"{os.path.basename(path)}  —  "
            f"{len(maps['DUR'])} DUR, {len(maps['AEP'])} AEP, {len(maps['TP'])} TP")
        self._tef_lbl.setStyleSheet("color:#166534; font-weight:bold;")
        # Wipe row-level overrides so preview picks up the new labels
        self._custom_stems.clear()
        self._refresh_preview()
        return True

    def _clear_tef(self):
        had_tef = self._tef_path is not None
        self._tef_path = None
        self._tef_maps = None
        self._tef_lbl.setText("(none loaded)")
        self._tef_lbl.setStyleSheet("color:#6b7280; font-style:italic;")
        self._custom_stems.clear()
        self._refresh_preview()
        if had_tef and not getattr(self, '_suspend_export_dirty', False):
            self._mark_set_dirty()

    def _on_name_template_changed(self, text):
        self._name_template = text.strip()
        # Template change invalidates prior per-row edits.
        self._custom_stems.clear()
        self._refresh_preview()
        if not getattr(self, '_suspend_export_dirty', False):
            self._mark_set_dirty()

    def _refresh_preview(self):
        self._exp_preview_table.blockSignals(True)
        self._exp_preview_table.setRowCount(0)
        has_shifted = False
        for source, aep, dur_label, tp_num, entry in self._get_export_events():
            if entry and entry.get('time_shifted'):
                has_shifted = True
            dur_min = entry['parsed'][2] if entry and entry.get('parsed') else None
            tmpl_stem = self._templated_stem(aep, dur_min, tp_num)
            default_stem = tmpl_stem or (self._entry_stem(entry) if entry else "—")
            key  = (source, aep, dur_label, tp_num)
            name = self._custom_stems.get(key, default_stem)
            row  = self._exp_preview_table.rowCount()
            self._exp_preview_table.insertRow(row)
            src_it = QTableWidgetItem(source)
            src_it.setFlags(ItemIsEnabled)
            src_it.setForeground(
                QColor('#2563eb') if source.startswith("C") else QColor('#ea580c'))
            src_it.setData(UserRole, entry)   # stored for context menu
            self._exp_preview_table.setItem(row, 0, src_it)
            for col, text in [(1, aep), (2, dur_label)]:
                it = QTableWidgetItem(text)
                it.setFlags(ItemIsEnabled)
                self._exp_preview_table.setItem(row, col, it)
            tp_it = QTableWidgetItem(f"TP{tp_num}")
            tp_it.setFlags(ItemIsEnabled)
            tp_it.setTextAlignment(AlignCenter)
            self._exp_preview_table.setItem(row, 3, tp_it)
            name_it = QTableWidgetItem(name)
            name_it.setData(UserRole, (key, default_stem))
            self._exp_preview_table.setItem(row, 4, name_it)
        self._exp_preview_table.blockSignals(False)
        if has_shifted:
            self._exp_version_warn.setText(
                "⚠ Warning: These results are from RORB <v6.52 — the time axis has been "
                "corrected by one time step (varies by duration) so that it starts at 0.00 h. "
                "RORB 6.52+ outputs are unaffected.")
            self._exp_version_warn.setVisible(True)
        else:
            self._exp_version_warn.setVisible(False)

    def _on_preview_name_changed(self, item):
        if item.column() != 4:
            return
        data = item.data(UserRole)
        if not data:
            return
        key, default = data
        val = item.text().strip()
        if val and val != default:
            self._custom_stems[key] = val
        else:
            self._custom_stems.pop(key, None)

    def _resolve_stem(self, source, aep, dur_label, tp_num, entry):
        key = (source, aep, dur_label, tp_num)
        if key in self._custom_stems:
            return self._custom_stems[key]
        dur_min = entry['parsed'][2] if entry and entry.get('parsed') else None
        tmpl = self._templated_stem(aep, dur_min, tp_num)
        return tmpl or self._entry_stem(entry)

    # ── Scenario management ───────────────────────────────────────────────────

    def add_scenario(self, name, folder):
        """
        Programmatic equivalent of _add_scenario() — used by the "Run RORB"
        dialog to load a freshly-produced .out folder without showing the
        Add Scenario prompt. Picks a free name if `name` is already taken.
        """
        if not folder or not os.path.isdir(folder):
            return
        base, candidate, n = name, name, 1
        while candidate in self._scenarios:
            n += 1
            candidate = f'{base} ({n})'
        self._scenarios[candidate] = {}
        self._scen_combo.addItem(candidate)
        self._scen_combo.setCurrentText(candidate)
        self._scan_scenario(candidate, folder)
        self._mark_set_dirty()

    def _add_scenario(self):
        dlg = _AddScenarioDialog(self)
        if dlg.exec() != DialogAccepted: return
        name, folder = dlg.values()
        if not name:
            QMessageBox.warning(self, "Add Scenario", "Enter a scenario name."); return
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "Add Scenario", "Select a valid folder."); return
        if name in self._scenarios:
            QMessageBox.warning(self, "Add Scenario",
                                f"Scenario '{name}' already exists."); return
        self._scenarios[name] = {}
        self._scen_combo.addItem(name)
        self._scen_combo.setCurrentText(name)
        self._scan_scenario(name, folder)
        self._mark_set_dirty()

    def _add_multiple_scenarios(self):
        dlg = _AddMultipleScenariosDialog(self, existing_names=self._scenarios.keys())
        if dlg.exec() != DialogAccepted:
            return
        for name, folder in dlg.values():
            # Reuse add_scenario() which auto-uniques the name and kicks off
            # a background scan for each folder.
            self.add_scenario(name, folder)

    # ── Scenario-set banner ──────────────────────────────────────────────────

    def _refresh_set_label(self):
        if not hasattr(self, '_set_label'):
            return
        if self._set_path:
            name = os.path.basename(self._set_path)
            if self._set_dirty:
                self._set_label.setText(f"Set:  {name}  •  unsaved changes")
                self._set_label.setStyleSheet(
                    "color:#dc2626; font-style:italic; font-weight:bold;"
                    "padding:2px 4px;")
            else:
                self._set_label.setText(f"Set:  {name}")
                self._set_label.setStyleSheet(
                    "color:#166534; font-weight:bold; padding:2px 4px;")
        else:
            if self._scenarios and self._set_dirty:
                self._set_label.setText("Set:  (unsaved)")
                self._set_label.setStyleSheet(
                    "color:#dc2626; font-style:italic; font-weight:bold;"
                    "padding:2px 4px;")
            else:
                self._set_label.setText("Set:  (none loaded)")
                self._set_label.setStyleSheet(
                    "color:#6b7280; font-style:italic; padding:2px 4px;")

    def _mark_set_dirty(self):
        self._set_dirty = True
        self._refresh_set_label()

    def _mark_set_clean(self, path=None):
        if path is not None:
            self._set_path = path
        self._set_dirty = False
        self._refresh_set_label()

    def _collect_scenario_pairs(self):
        return [{'name': n, 'folder': self._scenario_folders.get(n, '')}
                for n in self._scenarios.keys()
                if self._scenario_folders.get(n)]

    def _write_scenario_set(self, path, pairs):
        import json
        payload = {
            'scenarios': pairs,
            'export': {
                'tef_path': self._tef_path or '',
                'name_template': self._name_template or '',
            },
        }
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
        except OSError as ex:
            QMessageBox.critical(self, "Save Set", f"Could not save:\n{ex}")
            return False
        self._mark_set_clean(path)
        QMessageBox.information(
            self, "Save Set",
            f"Saved {len(pairs)} scenario(s) to:\n{path}")
        return True

    def _save_scenario_set(self):
        pairs = self._collect_scenario_pairs()
        if not pairs:
            QMessageBox.warning(self, "Save Set", "No scenarios to save."); return
        path = getattr(self, '_set_path', '') or ''
        if not path:
            self._save_scenario_set_as()
            return
        self._write_scenario_set(path, pairs)

    def _save_scenario_set_as(self):
        pairs = self._collect_scenario_pairs()
        if not pairs:
            QMessageBox.warning(self, "Save Set", "No scenarios to save."); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Scenario Set As", "", "Scenario set (*.json)")
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        self._write_scenario_set(path, pairs)

    def _load_scenario_set(self):
        import json
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Scenario Set", "", "Scenario set (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as ex:
            QMessageBox.critical(self, "Load Set", f"Could not read:\n{ex}")
            return
        # Accept either {"scenarios": [...]} or a bare list [...]
        pairs = data.get('scenarios', data) if isinstance(data, dict) else data
        if not isinstance(pairs, list) or not pairs:
            QMessageBox.warning(self, "Load Set", "File contains no scenarios.")
            return
        export_cfg = (data.get('export') if isinstance(data, dict) else None) or {}

        append = False
        if self._scenarios:
            resp = QMessageBox.question(
                self, "Load Set",
                "Replace the current scenarios, or append the loaded ones?\n\n"
                "Yes = Replace all,  No = Append,  Cancel = do nothing",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if resp == QMessageBox.Cancel:
                return
            if resp == QMessageBox.Yes:
                self._clear_all_scenarios()
            else:
                append = True

        valid, invalid = [], []
        for sc in pairs:
            if not isinstance(sc, dict):
                continue
            name   = str(sc.get('name', '')).strip()
            folder = str(sc.get('folder', '')).strip()
            if not name:
                continue
            if folder and os.path.isdir(folder):
                valid.append((name, folder))
            else:
                invalid.append((name, folder))

        # Let the user reassign any missing folders before loading.
        if invalid:
            dlg = _AddMultipleScenariosDialog(
                self,
                existing_names=list(self._scenarios.keys()) +
                               [n for n, _ in valid],
                initial_rows=invalid,
                title="Fix Missing Folders",
                intro=("These scenarios have missing or invalid folders. "
                       "Browse to the new location for each, remove rows to "
                       "skip them, then click 'Add All'."))
            if dlg.exec() == DialogAccepted:
                valid.extend(dlg.values())

        for name, folder in valid:
            self.add_scenario(name, folder)

        # Restore export naming settings on replace-load (skip on append so
        # the user's current tef/template survives). Suppress dirty marks so
        # a freshly-loaded set is not immediately shown as "unsaved".
        if not append:
            tef_saved = str(export_cfg.get('tef_path', '')).strip()
            tmpl_saved = str(export_cfg.get('name_template', ''))
            self._suspend_export_dirty = True
            try:
                self._clear_tef()
                self._name_template_edit.setText(tmpl_saved)
                if tef_saved and os.path.isfile(tef_saved):
                    self._apply_tef_path(tef_saved, show_errors=False)
            finally:
                self._suspend_export_dirty = False

        # A pure replace/load = clean state for this file. An append means the
        # on-disk file no longer matches memory, so it's dirty (and there is
        # no single "current" set path).
        if append:
            self._set_path = ''
            self._set_dirty = True
        else:
            self._set_path = path
            self._set_dirty = False
        self._refresh_set_label()

        loaded  = len(valid)
        skipped = len(pairs) - loaded
        msg = f"Loaded {loaded} scenario(s)."
        if skipped > 0:
            msg += f"\nSkipped {skipped}."
        QMessageBox.information(self, "Load Set", msg)

    def _rename_scenario(self):
        old = self._scen_combo.currentText()
        if not old: return
        new, ok = QInputDialog.getText(self, "Rename Scenario", "New name:", text=old)
        new = new.strip()
        if not ok or not new or new == old: return
        if new in self._scenarios:
            QMessageBox.warning(self, "Rename Scenario",
                                f"A scenario named '{new}' already exists."); return
        self._scenarios[new]        = self._scenarios.pop(old)
        self._scenario_folders[new] = self._scenario_folders.pop(old, '')
        if self._active == old:
            self._active = new
        idx = self._scen_combo.findText(old)
        self._scen_combo.blockSignals(True)
        self._scen_combo.setItemText(idx, new)
        self._scen_combo.blockSignals(False)
        self._refresh_all()
        self._mark_set_dirty()

    def _remove_scenario(self):
        name = self._scen_combo.currentText()
        if not name: return
        if QMessageBox.question(self, "Remove", f"Remove scenario '{name}'?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        del self._scenarios[name]
        self._scenario_folders.pop(name, None)
        idx = self._scen_combo.findText(name)
        self._scen_combo.removeItem(idx)
        self._refresh_all()
        self._mark_set_dirty()

    def _clear_all_scenarios(self):
        self._scenarios.clear()
        self._scenario_folders.clear()
        self._active = None
        self._scen_combo.blockSignals(True)
        self._scen_combo.clear()
        self._scen_combo.blockSignals(False)
        self._refresh_all()
        # A full clear also detaches from any loaded set.
        self._set_path = ''
        self._set_dirty = False
        self._refresh_set_label()

    def _clear_all_scenarios_prompt(self):
        if not self._scenarios:
            return
        if QMessageBox.question(self, "Clear All", "Remove all scenarios?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._clear_all_scenarios()

    def _scan_scenario(self, name, folder):
        self._scenario_folders[name] = folder
        self._scan_progress.setVisible(True)
        self._scan_progress.setValue(0)
        self._scan_status.setText(f"Scanning '{name}' …")
        self._worker = _ScanWorker(name, folder)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.result.connect(self._on_scan_done)
        self._worker.error.connect(self._on_scan_error)
        self._worker.start()

    def _on_scan_progress(self, cur, total, fname):
        self._scan_progress.setMaximum(total)
        self._scan_progress.setValue(cur)
        self._scan_status.setText(f"{cur}/{total}: {fname}")

    def _on_scan_done(self, scenario_name, files):
        self._scenarios[scenario_name] = files
        self._scan_progress.setVisible(False)
        ok = sum(1 for e in files.values() if e.get('parsed') and e.get('nodes'))
        self._scan_status.setText(
            f"'{scenario_name}': {ok}/{len(files)} files OK")
        self._refresh_all()

    def _on_scan_error(self, scenario_name, msg):
        self._scan_progress.setVisible(False)
        self._scan_status.setText(f"Error scanning '{scenario_name}'")
        QMessageBox.critical(self, "Scan error", msg)

    def _on_exp_scen_changed(self):
        # Clear extra events — they belong to the previous scenario
        self._exp_extra_rows.clear()
        self._exp_extra_table.setRowCount(0)
        self._refresh_export_combos()

    def _extra_table_context_menu(self, pos):
        from qgis.PyQt.QtWidgets import QMenu
        from qgis.PyQt.QtGui import QDesktopServices
        from qgis.PyQt.QtCore import QUrl

        row = self._exp_extra_table.indexAt(pos).row()
        if row < 0:
            return
        aep_it   = self._exp_extra_table.item(row, 0)
        out_path = aep_it.data(UserRole) if aep_it else ''
        if not out_path:
            return
        stm_path = os.path.splitext(out_path)[0] + '.stm'

        menu = QMenu(self._exp_extra_table)
        act_out = menu.addAction(
            f"Open .out  ({os.path.basename(out_path)})" if out_path else "Open .out")
        act_out.setEnabled(bool(out_path) and os.path.exists(out_path))
        act_stm = menu.addAction(
            f"Open .stm  ({os.path.basename(stm_path)})" if stm_path else "Open .stm")
        act_stm.setEnabled(bool(stm_path) and os.path.exists(stm_path))
        menu.addSeparator()
        act_dir = menu.addAction("Open containing folder")
        act_dir.setEnabled(bool(out_path))

        action = menu.exec(self._exp_extra_table.viewport().mapToGlobal(pos))
        if action == act_out and os.path.exists(out_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_path))
        elif action == act_stm and os.path.exists(stm_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(stm_path))
        elif action == act_dir and out_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(out_path)))

    def _on_rep_method_changed(self):
        self._populate_critical_table()
        self._replot()
        self._replot_envelope()
        self._refresh_exp_crit_table()
        self._refresh_preview()

    def _on_scenario_changed(self):
        self._active = self._scen_combo.currentText() or None
        self._sync_list_from_combo()
        self._refresh_all()

    # ── Scenario-list panel sync ─────────────────────────────────────────────

    def _sync_list_from_combo(self):
        """Mirror _scen_combo items and selection into the left-panel list."""
        if not hasattr(self, '_scen_list'):
            return
        combo_names = [self._scen_combo.itemText(i)
                       for i in range(self._scen_combo.count())]
        list_names  = [self._scen_list.item(i).text()
                       for i in range(self._scen_list.count())]
        self._suspend_reorder = True
        self._scen_list.blockSignals(True)
        if combo_names != list_names:
            self._scen_list.clear()
            for n in combo_names:
                self._scen_list.addItem(QListWidgetItem(n))
        cur = self._scen_combo.currentText()
        for i in range(self._scen_list.count()):
            if self._scen_list.item(i).text() == cur:
                self._scen_list.setCurrentRow(i)
                break
        self._scen_list.blockSignals(False)
        self._suspend_reorder = False

    def _on_scen_list_row_changed(self, row):
        if row < 0 or row >= self._scen_list.count():
            return
        name = self._scen_list.item(row).text()
        if name and name != self._scen_combo.currentText():
            self._scen_combo.setCurrentText(name)

    def _reorder_scenarios_from_list(self):
        """Called after a drag-drop reorder inside _scen_list — apply the new
        widget order to the underlying dicts, combo and downstream views."""
        if getattr(self, '_suspend_reorder', False):
            return
        n = self._scen_list.count()
        if n < 2:
            return
        names = [self._scen_list.item(i).text() for i in range(n)]
        current_order = list(self._scenarios.keys())
        if names == current_order:
            return
        if set(names) != set(current_order):
            return

        self._scenarios        = {k: self._scenarios[k]        for k in names}
        self._scenario_folders = {k: self._scenario_folders.get(k, '')
                                  for k in names}

        active = self._active
        self._scen_combo.blockSignals(True)
        self._scen_combo.clear()
        for k in names:
            self._scen_combo.addItem(k)
        if active in names:
            self._scen_combo.setCurrentText(active)
        self._scen_combo.blockSignals(False)

        self._mark_set_dirty()
        self._refresh_all()

    def _refresh_all(self):
        self._active = self._scen_combo.currentText() or None
        self._sync_list_from_combo()
        self._refresh_file_table()
        self._refresh_combos()
        self._refresh_compare_combos()

    def _refresh_combos(self):
        aeps      = self._all_aeps()
        all_nodes = self._all_nodes()

        self._aep_combo.blockSignals(True)
        self._aep_combo.clear()
        for a in aeps: self._aep_combo.addItem(a)
        self._aep_combo.blockSignals(False)

        for combo in (self._crit_node_combo,):
            combo.blockSignals(True); combo.clear()
            for n in all_nodes: combo.addItem(n)
            combo.blockSignals(False)

        names = list(self._scenarios.keys())
        self._exp_scen_combo.blockSignals(True); self._exp_scen_combo.clear()
        for n in names: self._exp_scen_combo.addItem(n)
        if self._active and self._active in names:
            self._exp_scen_combo.setCurrentText(self._active)
        self._exp_scen_combo.blockSignals(False)
        if hasattr(self, '_exp_scen_lbl'):
            self._exp_scen_lbl.setText(self._active or "(no scenario loaded)")

        self._refresh_export_combos()
        self._refresh_envelope_combos()

        if aeps:
            self._populate_critical_table()
            self._refresh_viewer()

    def _refresh_export_combos(self):
        scen  = self._exp_scenario()
        nodes = self._all_nodes(scen)
        prev  = self._exp_node_combo.currentText()
        self._exp_node_combo.blockSignals(True); self._exp_node_combo.clear()
        for n in nodes: self._exp_node_combo.addItem(n)
        if prev and self._exp_node_combo.findText(prev) >= 0:
            self._exp_node_combo.setCurrentText(prev)
        elif nodes:
            self._exp_node_combo.setCurrentIndex(len(nodes) - 1)  # default: last node (outlet)
        self._exp_node_combo.blockSignals(False)
        self._refresh_exp_crit_table()
        self._refresh_extra_aep()
        self._refresh_preview()

    def _refresh_compare_combos(self):
        if not hasattr(self, '_cmp_scen_vbox'):
            return

        names = list(self._scenarios.keys())
        if not self._cmp_scenario_rows:
            for i, name in enumerate(names[:4]):
                self._cmp_add_row(name)
            return

        for rd in self._cmp_scenario_rows:
            cur = rd['scen'].currentText()
            rd['scen'].blockSignals(True); rd['scen'].clear()
            for n in names: rd['scen'].addItem(n)
            if cur in names: rd['scen'].setCurrentText(cur)
            rd['scen'].blockSignals(False)
            self._cmp_refresh_node(rd['scen'], rd['node'])

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _scenario_color(self, name):
        names = list(self._scenarios.keys())
        idx = names.index(name) if name in names else 0
        return _SCENARIO_COLORS[idx % len(_SCENARIO_COLORS)]

    def _active_files(self, scenario=None):
        name = scenario or self._active
        return self._scenarios.get(name, {})

    def _valid_entries(self, scenario=None):
        return [e for e in self._active_files(scenario).values()
                if e.get('parsed') and e.get('nodes')]

    def _entries_for_aep(self, aep, scenario=None):
        return [e for e in self._valid_entries(scenario) if e['parsed'][0] == aep]

    def _all_aeps(self, scenario=None):
        return sorted(
            set(e['parsed'][0] for e in self._valid_entries(scenario)),
            key=_aep_sort_key)

    def _all_nodes(self, scenario=None):
        nodes = []
        for e in self._valid_entries(scenario):
            for n in e['nodes']:
                if n not in nodes: nodes.append(n)
        return nodes

    def _get_hydro(self, entry, node):
        nodes_d = entry.get('nodes', {})
        if not nodes_d: return None
        if node and node in nodes_d: return nodes_d[node]
        return list(nodes_d.values())[-1]

    def _compute_critical(self, aep, node, scenario=None):
        entries = self._entries_for_aep(aep, scenario)
        if not entries: return None
        dur_groups = defaultdict(list)
        for e in entries:
            _, dur_label, dur_min, tp = e['parsed']
            q    = self._get_hydro(e, node)
            peak = float(np.max(q)) if q is not None and len(q) else 0.0
            dur_groups[(dur_label, dur_min)].append((tp, peak, e))
        if not dur_groups: return None
        dur_means = {
            key: (float(np.mean([pk for _, pk, _ in tps])), tps)
            for key, tps in dur_groups.items()
        }
        (crit_lbl, crit_min), (mean_peak, crit_tps) = max(
            dur_means.items(), key=lambda x: x[1][0])
        method = self._rep_method.currentData() if hasattr(self, '_rep_method') else 'closest'
        if method == 'above':
            above = [(tp, pk, e) for tp, pk, e in crit_tps if pk >= mean_peak]
            pool  = above if above else crit_tps
            rep_tp_num, rep_peak, rep_entry = min(pool, key=lambda x: x[1] - mean_peak
                                                  if above else abs(x[1] - mean_peak))
        else:
            rep_tp_num, rep_peak, rep_entry = min(
                crit_tps, key=lambda x: abs(x[1] - mean_peak))
        return {
            'crit_dur': crit_lbl, 'crit_min': crit_min,
            'rep_tp': rep_tp_num, 'rep_peak': rep_peak,
            'mean_peak': mean_peak, 'n_tps': len(crit_tps),
            'rep_entry': rep_entry, 'crit_tps': crit_tps,
            'dur_means': {lbl: v for (lbl,_),(v,_) in dur_means.items()},
        }

    # ── Viewer ────────────────────────────────────────────────────────────────

    def _refresh_viewer(self):
        aep = self._aep_combo.currentText()
        if not aep: return
        entries = self._entries_for_aep(aep)
        if not entries: return
        all_nodes = []
        for e in entries:
            for n in e['nodes']:
                if n not in all_nodes: all_nodes.append(n)
        self._node_combo_v.blockSignals(True)
        self._node_combo_v.clear()
        for n in all_nodes: self._node_combo_v.addItem(n)
        self._node_combo_v.blockSignals(False)
        durs = sorted(set((e['parsed'][1], e['parsed'][2]) for e in entries),
                      key=lambda x: x[1])
        self._dur_combo.blockSignals(True); self._dur_combo.clear()
        self._dur_combo.addItem("Critical (auto)", None)
        for lbl, mins in durs: self._dur_combo.addItem(lbl, mins)
        self._dur_combo.blockSignals(False)
        self._rebuild_tp_checks(); self._replot()

    def _on_dur_changed(self):
        self._rebuild_tp_checks(); self._replot()

    def _rebuild_tp_checks(self):
        while self._tp_vbox.count():
            item = self._tp_vbox.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._tp_rows.clear()
        aep  = self._aep_combo.currentText()
        node = self._node_combo_v.currentText() or None
        if not aep: self._tp_vbox.addStretch(); return
        entries = self._entries_for_aep(aep)
        dur_min = self._dur_combo.currentData()
        if dur_min is None:
            crit = self._compute_critical(aep, node)
            if crit: dur_min = crit['crit_min']
        if dur_min is not None:
            entries = [e for e in entries if e['parsed'][2] == dur_min]
        for i, e in enumerate(sorted(entries, key=lambda x: x['parsed'][3])):
            tp_num = e['parsed'][3]; color = _TP_COLORS[i % len(_TP_COLORS)]
            row_w = QWidget(); row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(2,1,2,1)
            swatch = QLabel("  "); swatch.setFixedSize(12,16)
            swatch.setStyleSheet(f"background:{color};border:1px solid #555;")
            chk = QCheckBox(f"TP{tp_num}"); chk.setChecked(True)
            chk.stateChanged.connect(self._replot)
            row_l.addWidget(swatch); row_l.addWidget(chk); row_l.addStretch()
            self._tp_vbox.addWidget(row_w)
            self._tp_rows[tp_num] = (chk, color)
        self._tp_vbox.addStretch()

    def _toggle_all_tps(self, state):
        for chk, _ in self._tp_rows.values(): chk.setChecked(state)

    def _replot(self):
        if not HAS_MPL or not self._scenarios: return
        aep  = self._aep_combo.currentText()
        node = self._node_combo_v.currentText() or None
        if not aep: return
        entries = self._entries_for_aep(aep)
        dur_min = self._dur_combo.currentData()
        crit    = self._compute_critical(aep, node)
        if dur_min is None and crit: dur_min = crit['crit_min']
        if dur_min is not None:
            entries = [e for e in entries if e['parsed'][2] == dur_min]
        self._ax.clear()
        self._ax2.clear()

        # Update "Critical" combo label with the actual critical duration
        self._dur_combo.blockSignals(True)
        self._dur_combo.setItemText(
            0, f'Critical ({crit["crit_dur"]})' if crit else 'Critical (auto)')
        self._dur_combo.blockSignals(False)

        # Representative TP for the currently displayed duration (not necessarily the
        # overall critical one — each duration gets its own closest-to-mean TP)
        rep_tp = None
        rep_tp_peak = None
        if crit:
            if dur_min == crit['crit_min']:
                rep_tp, rep_tp_peak = crit['rep_tp'], crit['rep_peak']
            elif entries:
                _method = (self._rep_method.currentData()
                           if hasattr(self, '_rep_method') else 'closest')
                _peaks = []
                for _e in entries:
                    _q = self._get_hydro(_e, node)
                    _pk = float(np.max(_q)) if _q is not None and len(_q) else 0.0
                    _peaks.append((_e['parsed'][3], _pk))
                if _peaks:
                    _mean = sum(p for _, p in _peaks) / len(_peaks)
                    if _method == 'above':
                        _above = [(tp, pk) for tp, pk in _peaks if pk >= _mean]
                        _pool = _above if _above else _peaks
                        rep_tp, rep_tp_peak = min(_pool, key=lambda x: abs(x[1] - _mean))
                    else:
                        rep_tp, rep_tp_peak = min(_peaks, key=lambda x: abs(x[1] - _mean))
        active_peaks = []
        for e in sorted(entries, key=lambda x: x['parsed'][3]):
            tp_num = e['parsed'][3]
            chk, color = self._tp_rows.get(tp_num, (None, '#888'))
            if chk and not chk.isChecked(): continue
            q = self._get_hydro(e, node)
            if q is None: continue
            t = e.get('time', [])[:len(q)]
            is_rep = self._hilite_chk.isChecked() and (tp_num == rep_tp)
            self._ax.plot(t, q, color=color, linewidth=2.5 if is_rep else 1.2,
                          label=f"TP{tp_num}" + (" ★" if is_rep else ""),
                          alpha=1.0 if is_rep else 0.70, zorder=4 if is_rep else 2)
            active_peaks.append(float(np.max(q)))
        if self._mean_chk.isChecked() and active_peaks:
            mean_pk = float(np.mean(active_peaks))
            self._ax.axhline(mean_pk, color='#111827', linewidth=1.4,
                             linestyle='--', zorder=5,
                             label=f"Mean: {mean_pk:.3f} m³/s")
        # Rainfall bars for the representative TP (same convention as Critical Events)
        rep_entry = next((e for e in entries if e['parsed'][3] == rep_tp), None)
        if rep_entry is None and entries:
            rep_entry = entries[0]
        if rep_entry is not None:
            rain_t  = rep_entry.get('rain_t',  [])
            rain_mm = rep_entry.get('rain_mm', [])
            if len(rain_t) >= 2 and len(rain_mm) == len(rain_t):
                dt = rain_t[1] - rain_t[0]
                self._ax2.bar(rain_t, rain_mm, width=dt, align='edge',
                              color='#3b82f6', alpha=0.35, zorder=1,
                              label=f"Rainfall (TP{rep_entry['parsed'][3]})")
                self._ax2.yaxis.set_label_position('right')
                self._ax2.yaxis.tick_right()
                self._ax2.set_ylabel("Rainfall (mm)", color='#3b82f6')
                self._ax2.tick_params(axis='y', labelcolor='#3b82f6')
                max_rain = max(rain_mm) if rain_mm else 1
                self._ax2.set_ylim(max_rain * 4, 0)   # inverted
            else:
                self._ax2.set_yticks([])
        else:
            self._ax2.set_yticks([])

        dur_txt = self._dur_combo.currentText()
        self._ax.set_xlabel("Time (hr)"); self._ax.set_ylabel("Flow (m³/s)")
        scen = self._active or ''
        prefix = f"{scen}  |  " if scen else ""
        self._ax.set_title(f"{prefix}{aep}  |  {dur_txt}  |  {node or 'outlet'}",
                           fontsize=10)
        self._ax.grid(True, alpha=0.25)
        if self._ax.lines: self._ax.legend(fontsize=7.5, loc='upper right')
        all_q = []
        for e in entries:
            q = self._get_hydro(e, node)
            if q is not None: all_q.extend(list(q))
        if all_q and entries:
            t_ref = entries[0].get('time', [])
            pk_idx = int(np.argmax(all_q[:len(t_ref)]))
            pk_t = t_ref[pk_idx] if pk_idx < len(t_ref) else 10
            xmax = min(t_ref[-1] if t_ref else 100, pk_t*3+2)
            # Make sure the rainfall bars are visible even when flow is ~0
            # (auto-crop would otherwise collapse to the storm-start region).
            if rep_entry is not None:
                rain_t = rep_entry.get('rain_t', [])
                if rain_t:
                    xmax = max(xmax, rain_t[-1])
            self._ax.set_xlim(0, xmax)

        self._canvas.draw()
        summary_lines = []
        if crit:
            summary_lines += [
                f"Critical: {crit['crit_dur']}",
                f"Mean pk:  {crit['mean_peak']:.3f} m³/s",
            ]
            if rep_tp is not None:
                pk_s = f'  ({rep_tp_peak:.3f} m³/s)' if rep_tp_peak is not None else ''
                summary_lines.append(f"Rep TP:   TP{rep_tp}{pk_s}")
        if any(e.get('time_shifted') for e in entries):
            summary_lines.append("⚠ RORB <v6.52 — time axis corrected")
        self._peak_summary.setText("\n".join(summary_lines))

    # ── Export ────────────────────────────────────────────────────────────────

    def _export_hydros(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first."); return
        node   = self._exp_node_combo.currentText() or None
        events = self._get_export_events()
        if not events:
            QMessageBox.warning(self, "Export", "No events selected."); return
        saved = []
        adjusted = []
        for source, aep, dur_label, tp_num, entry in events:
            q = self._get_hydro(entry, node)
            t = list(entry.get('time', [])[:len(q)]) if q is not None else []
            if q is None: continue
            q = list(q)
            if t:
                dt = entry.get('dt') or (t[1] - t[0] if len(t) >= 2 else t[0])
                if t[0] > 0:
                    t = [0.0] + t
                    q = [0.0] + q
                t.append(float(t[-1]) + dt)
                q.append(0.0)
            stem  = self._resolve_stem(source, aep, dur_label, tp_num, entry)
            fname = os.path.join(folder, f"{stem}_hydro.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Flow (cms)"])
                for tv, qv in zip(t, q):
                    w.writerow([f"{tv:.4f}", f"{float(qv):.6f}"])
            saved.append(os.path.basename(fname))
            if entry.get('time_shifted'):
                dt_val = entry.get('dt')
                dt_str = f"{dt_val * 60:.4g} min" if dt_val else "?"
                adjusted.append((os.path.basename(fname), dt_str))
        msg = f"Exported {len(saved)} file(s) to:\n{folder}"
        if adjusted:
            file_lines = "\n".join(f"  {fn}  (step: {ds})" for fn, ds in adjusted)
            msg += (
                f"\n\nNote: {len(adjusted)} file(s) from RORB <v6.52 had their time axis "
                f"shifted back by one time step so that time starts at 0.00 h "
                f"(RORB <v6.52 omits the Inc 0 row):\n" + file_lines
            )
        QMessageBox.information(self, "Export Hydrographs", msg)

    def _export_hyetos(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first."); return
        events = self._get_export_events()
        if not events:
            QMessageBox.warning(self, "Export", "No events selected."); return
        saved, skipped = [], []
        for source, aep, dur_label, tp_num, entry in events:
            rain_t  = entry.get('rain_t',  [])
            rain_mm = entry.get('rain_mm', [])
            if not rain_mm: skipped.append(f"{aep} TP{tp_num}"); continue
            stem  = self._resolve_stem(source, aep, dur_label, tp_num, entry)
            fname = os.path.join(folder, f"{stem}_rf.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Rainfall (mm)"])
                for tv, rv in zip(rain_t, rain_mm):
                    w.writerow([f"{tv:.4f}", f"{rv:.4f}"])
            saved.append(os.path.basename(fname))
        msg = f"Exported {len(saved)} file(s) to:\n{folder}"
        if skipped: msg += f"\n\nNo rainfall data: {', '.join(skipped)}"
        QMessageBox.information(self, "Export Hyetographs", msg)

    # ── Excel export ──────────────────────────────────────────────────────────

    @staticmethod
    def _safe_sheet_name(stem, used):
        """Excel sheet names: <=31 chars, no []:*?/\\, unique within a book."""
        name = re.sub(r'[\[\]:*?/\\]', '_', stem).strip() or "case"
        name = name[:31]
        if name in used:
            for n in range(2, 100):
                suffix = f"_{n}"
                cand = name[:31 - len(suffix)] + suffix
                if cand not in used:
                    name = cand
                    break
        used.add(name)
        return name

    def _export_excel(self):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            QMessageBox.warning(
                self, "Export Excel",
                "openpyxl is not available in this Python environment, so .xlsx "
                "cannot be written.\n\nThe CSV exports work regardless.")
            return

        events = self._get_export_events()
        if not events:
            QMessageBox.warning(self, "Export", "No events selected."); return

        folder = self._exp_folder_edit.text().strip()
        scen   = self._exp_scenario() or ""
        default = os.path.join(folder, f"{scen or 'RORB'}_results.xlsx") if folder else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Excel", default, "Excel workbook (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith('.xlsx'):
            path += '.xlsx'

        from .core.engine import parse_out_params, parse_out_peaks

        node    = self._exp_node_combo.currentText() or None
        bold    = Font(bold=True)
        title_f = Font(bold=True, size=12)

        wb = Workbook()
        wb.remove(wb.active)          # drop the default empty sheet
        used_names = set()
        skipped    = []

        for source, aep, dur_label, tp_num, entry in events:
            q = self._get_hydro(entry, node)
            if q is None:
                skipped.append(f"{aep} {dur_label} TP{tp_num}")
                continue
            stem = self._resolve_stem(source, aep, dur_label, tp_num, entry)
            ws   = wb.create_sheet(self._safe_sheet_name(stem, used_names))

            out_path = entry.get('path', '')
            params   = parse_out_params(out_path) if out_path else {}
            peaks    = parse_out_peaks(out_path)  if out_path else {}
            node_pk  = peaks.get(node) if node else None
            if node_pk is None and peaks:
                node_pk = list(peaks.values())[-1]

            # ── Case identity + inputs, at the top of this case's sheet ────
            ws.cell(row=1, column=1, value=f"RORB case: {stem}").font = title_f
            r = 3
            for label, val in (
                    ("Scenario",         scen),
                    ("Source",           "Critical event" if source.startswith("C") else "Extra event"),
                    ("AEP",              aep),
                    ("Duration",         dur_label),
                    ("Temporal pattern", f"TP{tp_num}"),
                    ("Node",             node or "outlet"),
            ):
                ws.cell(row=r, column=1, value=label).font = bold
                ws.cell(row=r, column=2, value=val)
                r += 1

            r += 1
            ws.cell(row=r, column=1, value="Inputs").font = title_f
            r += 1
            for label, key, fmt in (
                    ("kc",                    'kc',             '0.000'),
                    ("m",                     'm',              '0.000'),
                    ("Initial loss (mm)",     'il',             '0.000'),
                    ("Continuing loss (mm/h)", 'cl',            '0.000'),
                    ("Loss model",            'loss_model',     None),
                    ("Total area (km²)",      'total_area_km2', '0.000'),
                    ("Av. distance (km)",     'avg_dist_km',    '0.000'),
                    ("Time increment (hr)",   'dt_hr',          '0.0000'),
                    ("Storm title",           'storm_title',    None),
                    ("Vector file (.catg)",   'catg_path',      None),
                    ("Storm file (.stm)",     'stm_path',       None),
                    ("RORB version",          'rorb_version',   None),
                    ("Date run",              'run_date',       None),
            ):
                ws.cell(row=r, column=1, value=label).font = bold
                c = ws.cell(row=r, column=2, value=params.get(key))
                if fmt and params.get(key) is not None:
                    c.number_format = fmt
                r += 1
            ws.cell(row=r, column=1, value="Source .out").font = bold
            ws.cell(row=r, column=2, value=os.path.basename(out_path))
            r += 1
            if entry.get('time_shifted'):
                c = ws.cell(row=r, column=1,
                            value="Note: RORB <v6.52 — time axis shifted back one "
                                  "step so time starts at 0.00 h")
                c.font = Font(italic=True, color="B45309")
                r += 1

            # ── Results at the selected node ──────────────────────────────
            r += 1
            ws.cell(row=r, column=1, value=f"Results at {node or 'outlet'}").font = title_f
            r += 1
            if node_pk:
                for label, key, fmt in (("Peak discharge (m³/s)", 'peak',   '0.000'),
                                        ("Time to peak (hr)",     'ttp',    '0.00'),
                                        ("Volume (m³)",           'volume', '0.000E+00')):
                    ws.cell(row=r, column=1, value=label).font = bold
                    c = ws.cell(row=r, column=2, value=node_pk.get(key))
                    if node_pk.get(key) is not None:
                        c.number_format = fmt
                    r += 1
            else:
                ws.cell(row=r, column=1, value="Peak discharge (m³/s)").font = bold
                c = ws.cell(row=r, column=2, value=float(np.max(q)))
                c.number_format = '0.000'
                r += 1

            # ── Time series ───────────────────────────────────────────────
            r += 1
            hdr = r
            for col, text in ((1, "Time (hr)"), (2, "Flow (m³/s)"),
                              (4, "Rain time (hr)"), (5, "Rainfall (mm)")):
                c = ws.cell(row=hdr, column=col, value=text)
                c.font = bold
                c.alignment = Alignment(horizontal='center')

            t = list(entry.get('time', [])[:len(q)])
            qq = [float(v) for v in q]
            if t:
                dt = entry.get('dt') or (t[1] - t[0] if len(t) >= 2 else t[0])
                if t[0] > 0:
                    t = [0.0] + t; qq = [0.0] + qq
                t.append(float(t[-1]) + dt); qq.append(0.0)
            for i, (tv, qv) in enumerate(zip(t, qq)):
                ws.cell(row=hdr + 1 + i, column=1, value=float(tv)).number_format = '0.0000'
                ws.cell(row=hdr + 1 + i, column=2, value=qv).number_format = '0.000'

            rain_t  = entry.get('rain_t',  [])
            rain_mm = entry.get('rain_mm', [])
            for i, (tv, rv) in enumerate(zip(rain_t, rain_mm)):
                ws.cell(row=hdr + 1 + i, column=4, value=float(tv)).number_format = '0.0000'
                ws.cell(row=hdr + 1 + i, column=5, value=float(rv)).number_format = '0.000'

            ws.column_dimensions['A'].width = 24
            ws.column_dimensions['B'].width = 30
            for letter in ('D', 'E'):
                ws.column_dimensions[letter].width = 15
            ws.freeze_panes = ws.cell(row=hdr + 1, column=1)

        if not wb.sheetnames:
            QMessageBox.warning(self, "Export Excel", "Nothing to export."); return
        try:
            wb.save(path)
        except OSError as e:
            QMessageBox.critical(
                self, "Export Excel",
                f"Could not write the workbook:\n{e}\n\n"
                "If the file is open in Excel, close it and try again.")
            return

        msg = (f"Exported {len(wb.sheetnames)} case sheet(s) to:\n{path}\n\n"
               f"Each sheet holds that case's inputs at the top, then its "
               f"hydrograph and hyetograph.")
        if skipped:
            msg += f"\n\nSkipped (no hydrograph): {', '.join(skipped)}"
        QMessageBox.information(self, "Export Excel", msg)

    def _export_temporal(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first."); return
        events = self._get_export_events()
        if not events:
            QMessageBox.warning(self, "Export", "No events selected."); return
        saved, skipped = [], []
        for source, aep, dur_label, tp_num, entry in events:
            rain_t  = entry.get('rain_t',  [])
            rain_mm = entry.get('rain_mm', [])
            if not rain_mm:
                skipped.append(f"{aep} TP{tp_num}"); continue
            total = sum(rain_mm)
            if total == 0:
                skipped.append(f"{aep} TP{tp_num}"); continue
            pcts = [r / total * 100.0 for r in rain_mm]
            stem  = self._resolve_stem(source, aep, dur_label, tp_num, entry)
            fname = os.path.join(folder, f"{stem}_tp.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Pattern (%)"])
                for tv, pv in zip(rain_t, pcts):
                    w.writerow([f"{tv:.4f}", f"{pv:.4f}"])
            saved.append(os.path.basename(fname))
        msg = f"Exported {len(saved)} file(s) to:\n{folder}"
        if skipped: msg += f"\n\nNo rainfall data: {', '.join(skipped)}"
        QMessageBox.information(self, "Export Temporal Patterns", msg)
