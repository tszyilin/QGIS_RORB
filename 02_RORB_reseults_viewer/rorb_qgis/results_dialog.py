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
    QRadioButton, QButtonGroup,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QFont, QColor

from .compat import (
    AllDockWidgetAreas, RightDockWidgetArea,
    AlignRightVCenter, AlignCenter,
    Horizontal, Vertical,
    CustomContextMenu,
    NoEditTriggers, SelectRows, HeaderStretch,
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
        self._cmp_scenario_rows = []
        self._build_ui()
        self._restore_state()
        from qgis.core import QgsProject
        proj = QgsProject.instance()
        proj.cleared.connect(self._clear_all_scenarios)
        proj.readProject.connect(self._clear_all_scenarios)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        _container = QWidget()
        _container.setMinimumSize(1150, 800)
        self.setWidget(_container)
        root = QVBoxLayout(_container)

        # ── Scenario bar ──────────────────────────────────────────────────
        sbar = QHBoxLayout()
        sbar.addWidget(QLabel("Scenario:"))
        self._scen_combo = QComboBox(); self._scen_combo.setMinimumWidth(200)
        self._scen_combo.currentIndexChanged.connect(self._on_scenario_changed)
        add_btn = QPushButton("Add…");    add_btn.setFixedWidth(60)
        ren_btn = QPushButton("Rename…"); ren_btn.setFixedWidth(75)
        rem_btn = QPushButton("Remove");  rem_btn.setFixedWidth(70)
        clr_btn = QPushButton("Clear");   clr_btn.setFixedWidth(60)
        add_btn.clicked.connect(self._add_scenario)
        ren_btn.clicked.connect(self._rename_scenario)
        rem_btn.clicked.connect(self._remove_scenario)
        clr_btn.clicked.connect(self._clear_all_scenarios_prompt)
        self._scan_progress = QProgressBar(); self._scan_progress.setVisible(False)
        self._scan_progress.setMaximumWidth(200)
        self._scan_status = QLabel("")
        self._scan_status.setStyleSheet("color:gray;font-size:8pt;")
        sbar.addWidget(self._scen_combo)
        sbar.addWidget(add_btn); sbar.addWidget(ren_btn); sbar.addWidget(rem_btn)
        sbar.addWidget(clr_btn)
        sbar.addWidget(self._scan_progress)
        sbar.addWidget(self._scan_status)
        sbar.addStretch()
        sbar.addWidget(QLabel("Rep TP:"))
        self._rep_method = QComboBox()
        self._rep_method.addItem("Closest to mean",   "closest")
        self._rep_method.addItem("Closest ≥ mean",    "above")
        self._rep_method.setFixedWidth(150)
        self._rep_method.currentIndexChanged.connect(self._on_rep_method_changed)
        sbar.addWidget(self._rep_method)
        root.addLayout(sbar)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_files(),    "Files")
        self._tabs.addTab(self._tab_critical(), "Critical Events")
        self._tabs.addTab(self._tab_viewer(),   "Hydrograph Viewer")
        self._tabs.addTab(self._tab_export(),   "Export")
        root.addWidget(self._tabs)

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
                self._file_table.setItem(row, 1, QTableWidgetItem(e['fname']))
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
            self._crit_ax.set_xlim(0, min(t[-1], pk_t * 3 + 2))
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

    # ── Tab 3: Hydrograph Viewer ──────────────────────────────────────────────

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
        self._exp_scen_combo = QComboBox(); self._exp_scen_combo.setMinimumWidth(200)
        self._exp_scen_combo.currentIndexChanged.connect(self._on_exp_scen_changed)
        form.addRow("Scenario:", self._exp_scen_combo)
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
        brow.addWidget(hydro_btn); brow.addWidget(hyeto_btn); brow.addStretch()
        lay.addLayout(brow)
        return w

    # ── Export helpers ────────────────────────────────────────────────────────

    def _browse_export_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder", "")
        if folder:
            self._exp_folder_edit.setText(folder)
            self._save_state()

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
        self._exp_extra_table.setItem(row, 0, QTableWidgetItem(aep))
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

    def _refresh_preview(self):
        self._exp_preview_table.blockSignals(True)
        self._exp_preview_table.setRowCount(0)
        has_shifted = False
        for source, aep, dur_label, tp_num, entry in self._get_export_events():
            if entry and entry.get('time_shifted'):
                has_shifted = True
            default_stem = self._entry_stem(entry) if entry else "—"
            key  = (source, aep, dur_label, tp_num)
            name = self._custom_stems.get(key, default_stem)
            row  = self._exp_preview_table.rowCount()
            self._exp_preview_table.insertRow(row)
            src_it = QTableWidgetItem(source)
            src_it.setFlags(ItemIsEnabled)
            src_it.setForeground(
                QColor('#2563eb') if source.startswith("C") else QColor('#ea580c'))
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
        return self._custom_stems.get(key, self._entry_stem(entry))

    # ── Session persistence ───────────────────────────────────────────────────

    def _save_state(self):
        from qgis.core import QgsSettings
        import json
        s = QgsSettings()
        saved = [{'name': n, 'folder': f}
                 for n, f in self._scenario_folders.items()
                 if n in self._scenarios and os.path.isdir(f)]
        s.setValue('rorb_qgis/results_viewer/scenarios',   json.dumps(saved))
        s.setValue('rorb_qgis/results_viewer/active',      self._active or '')
        s.setValue('rorb_qgis/results_viewer/rep_method',
                   self._rep_method.currentData() or 'closest')
        s.setValue('rorb_qgis/results_viewer/export_folder',
                   self._exp_folder_edit.text())

    def _restore_state(self):
        from qgis.core import QgsSettings
        import json
        s = QgsSettings()

        rep = s.value('rorb_qgis/results_viewer/rep_method', 'closest')
        idx = self._rep_method.findData(rep)
        if idx >= 0:
            self._rep_method.blockSignals(True)
            self._rep_method.setCurrentIndex(idx)
            self._rep_method.blockSignals(False)

        folder = s.value('rorb_qgis/results_viewer/export_folder', '')
        if folder:
            self._exp_folder_edit.setText(folder)

        raw = s.value('rorb_qgis/results_viewer/scenarios', '[]')
        try:
            saved = json.loads(raw)
        except Exception:
            saved = []

        active = s.value('rorb_qgis/results_viewer/active', '')
        for sc in saved:
            name   = sc.get('name', '')
            folder = sc.get('folder', '')
            if name and folder and os.path.isdir(folder):
                self._scenarios[name] = {}
                self._scen_combo.addItem(name)
                self._scan_scenario(name, folder)

        if active:
            idx = self._scen_combo.findText(active)
            if idx >= 0:
                self._scen_combo.setCurrentIndex(idx)

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
        self._save_state()

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
        self._save_state()

    def _clear_all_scenarios(self):
        self._scenarios.clear()
        self._scenario_folders.clear()
        self._active = None
        self._scen_combo.blockSignals(True)
        self._scen_combo.clear()
        self._scen_combo.blockSignals(False)
        self._refresh_all()
        self._save_state()

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
        self._save_state()

    def _on_scan_error(self, scenario_name, msg):
        self._scan_progress.setVisible(False)
        self._scan_status.setText(f"Error scanning '{scenario_name}'")
        QMessageBox.critical(self, "Scan error", msg)

    def _on_exp_scen_changed(self):
        self._refresh_export_combos()

    def _on_rep_method_changed(self):
        self._populate_critical_table()
        self._replot()
        self._refresh_exp_crit_table()
        self._refresh_preview()
        self._save_state()

    def _on_scenario_changed(self):
        self._active = self._scen_combo.currentText() or None
        self._refresh_all()

    def _refresh_all(self):
        self._active = self._scen_combo.currentText() or None
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
        cur_exp = self._exp_scen_combo.currentText()
        self._exp_scen_combo.blockSignals(True); self._exp_scen_combo.clear()
        for n in names: self._exp_scen_combo.addItem(n)
        if cur_exp in names: self._exp_scen_combo.setCurrentText(cur_exp)
        elif self._active and self._active in names:
            self._exp_scen_combo.setCurrentText(self._active)
        self._exp_scen_combo.blockSignals(False)

        self._refresh_export_combos()

        if aeps:
            self._populate_critical_table()
            self._refresh_viewer()

    def _refresh_export_combos(self):
        scen  = self._exp_scenario()
        nodes = self._all_nodes(scen)
        self._exp_node_combo.blockSignals(True); self._exp_node_combo.clear()
        for n in nodes: self._exp_node_combo.addItem(n)
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
        rep_tp = crit['rep_tp'] if crit else None
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
        dur_txt = self._dur_combo.currentText()
        self._ax.set_xlabel("Time (hr)"); self._ax.set_ylabel("Flow (m³/s)")
        self._ax.set_title(f"{aep}  |  {dur_txt}  |  {node or 'outlet'}", fontsize=10)
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
            self._ax.set_xlim(0, min(t_ref[-1] if t_ref else 100, pk_t*3+2))

        self._canvas.draw()
        summary_lines = []
        if crit:
            summary_lines += [
                f"Critical: {crit['crit_dur']}",
                f"Mean pk:  {crit['mean_peak']:.3f} m³/s",
                f"Rep TP:   TP{crit['rep_tp']}  ({crit['rep_peak']:.3f} m³/s)",
            ]
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
