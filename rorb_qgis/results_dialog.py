"""
RORB Results Viewer — multi-scenario edition.

Each scenario is a named folder of RORBWin .out files.  All existing tabs
(Files, Critical Events, Hydrograph Viewer, Export) operate on the active
scenario.  A new Compare tab overlays two scenarios' critical hydrographs
and shows a Δ-peak table for every AEP.

Filename pattern:  {prefix}aep{N}_du{dur}(min|hour)tp{M}.out
"""

import os
import re
import csv as csv_mod
import traceback
from collections import defaultdict

import numpy as np

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QTabWidget, QWidget,
    QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox,
    QLineEdit, QCheckBox, QScrollArea,
    QProgressBar, QSplitter, QInputDialog,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QFont, QColor

try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    HAS_MPL = True
except Exception:
    HAS_MPL = False

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
    m = re.search(r'aep(\d+)(?:_(\d+))?', stem)
    if m:
        whole = int(m.group(1)); frac = m.group(2)
        val = f"{whole}.{frac}" if frac else str(whole)
        aep_label = f"{val}% AEP"
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
    m = re.search(r'([\d.]+)', aep_label)
    return -float(m.group(1)) if m else 0.0


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
            from .core.engine import parse_out_hydrograph, parse_out_rainfall
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
                    nodes, time_axis, dt = parse_out_hydrograph(path)
                except Exception:
                    nodes, time_axis, dt = {}, [], None
                try:
                    rain_t, rain_mm, _ = parse_out_rainfall(path)
                except Exception:
                    rain_t, rain_mm = [], []
                return fname, path, parsed, nodes, time_axis, dt, rain_t, rain_mm

            total = len(fnames); done = 0; out = {}
            with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
                futures = {ex.submit(_parse_one, f): f for f in fnames}
                for fut in futures:
                    fname, path, parsed, nodes, time_axis, dt, rain_t, rain_mm = fut.result()
                    done += 1
                    self.progress.emit(done, total, fname)
                    out[path] = {
                        'fname': fname, 'path': path, 'parsed': parsed,
                        'nodes': nodes, 'time': time_axis, 'dt': dt,
                        'rain_t': rain_t, 'rain_mm': rain_mm,
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

class RorbResultsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RORB Results Viewer")
        self.setMinimumSize(1150, 800)
        self._scenarios  = {}   # name -> files dict
        self._active     = None
        self._worker     = None
        self._tp_rows    = {}
        self._crit_rows  = []
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        # ── Scenario bar ──────────────────────────────────────────────────
        sbar = QHBoxLayout()
        sbar.addWidget(QLabel("Scenario:"))
        self._scen_combo = QComboBox(); self._scen_combo.setMinimumWidth(200)
        self._scen_combo.currentIndexChanged.connect(self._on_scenario_changed)
        add_btn = QPushButton("Add…");    add_btn.setFixedWidth(60)
        rem_btn = QPushButton("Remove");  rem_btn.setFixedWidth(70)
        add_btn.clicked.connect(self._add_scenario)
        rem_btn.clicked.connect(self._remove_scenario)
        self._scan_progress = QProgressBar(); self._scan_progress.setVisible(False)
        self._scan_progress.setMaximumWidth(200)
        self._scan_status = QLabel("")
        self._scan_status.setStyleSheet("color:gray;font-size:8pt;")
        sbar.addWidget(self._scen_combo)
        sbar.addWidget(add_btn); sbar.addWidget(rem_btn)
        sbar.addWidget(self._scan_progress)
        sbar.addWidget(self._scan_status)
        sbar.addStretch()
        root.addLayout(sbar)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_files(),    "Files")
        self._tabs.addTab(self._tab_critical(), "Critical Events")
        self._tabs.addTab(self._tab_viewer(),   "Hydrograph Viewer")
        self._tabs.addTab(self._tab_compare(),  "Compare")
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
        self._file_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._file_table.setEditTriggers(QTableWidget.NoEditTriggers)
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
                    tpi = QTableWidgetItem(str(tp)); tpi.setTextAlignment(Qt.AlignCenter)
                    self._file_table.setItem(row, 4, tpi)
                    ok = bool(e.get('nodes'))
                    si2 = QTableWidgetItem("OK" if ok else "No hydrograph")
                    si2.setForeground(QColor('#16a34a' if ok else '#dc2626'))
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
        self._crit_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._crit_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._crit_table.setAlternatingRowColors(True)
        self._crit_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._crit_table.setMaximumHeight(220)
        self._crit_table.selectionModel().selectionChanged.connect(
            self._on_crit_row_selected)
        lay.addWidget(self._crit_table)

        if HAS_MPL:
            self._crit_fig    = Figure(figsize=(8, 3.5), tight_layout=True)
            self._crit_ax     = self._crit_fig.add_subplot(111)
            self._crit_canvas = FigureCanvas(self._crit_fig)
            lay.addWidget(self._crit_canvas)
        else:
            self._crit_ax = self._crit_canvas = None
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
            t = rep_e.get('time', [])
            ttp = 0.0
            if q is not None and len(q):
                pk_idx = int(np.argmax(q))
                ttp = t[pk_idx] if pk_idx < len(t) else 0.0
            crit['ttp'] = ttp; crit['aep'] = aep; crit['node'] = node
            self._crit_rows.append(crit)
            row = self._crit_table.rowCount()
            self._crit_table.insertRow(row)
            self._crit_table.setItem(row, 0, QTableWidgetItem(aep))
            self._crit_table.setItem(row, 1, QTableWidgetItem(crit['crit_dur']))
            tp_it = QTableWidgetItem(str(crit['rep_tp']))
            tp_it.setTextAlignment(Qt.AlignCenter)
            self._crit_table.setItem(row, 2, tp_it)
            n_it = QTableWidgetItem(str(crit['n_tps']))
            n_it.setTextAlignment(Qt.AlignCenter)
            self._crit_table.setItem(row, 3, n_it)
            for col, val, dec in (
                    (4, crit['mean_peak'], 3),
                    (5, crit['rep_peak'],  3),
                    (6, ttp,               2)):
                it = QTableWidgetItem(f"{val:.{dec}f}")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
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
        self._crit_canvas.draw()

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

        splitter = QSplitter(Qt.Horizontal)
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

    # ── Tab 4: Compare ────────────────────────────────────────────────────────

    def _tab_compare(self):
        w = QWidget(); root = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Scenario A:"))
        self._cmp_a = QComboBox(); self._cmp_a.setMinimumWidth(160)
        ctrl.addWidget(self._cmp_a)
        ctrl.addWidget(QLabel("Scenario B:"))
        self._cmp_b = QComboBox(); self._cmp_b.setMinimumWidth(160)
        ctrl.addWidget(self._cmp_b)
        ctrl.addWidget(QLabel("Node:"))
        self._cmp_node = QComboBox(); self._cmp_node.setMinimumWidth(140)
        ctrl.addWidget(self._cmp_node)
        run_btn = QPushButton("Compare"); run_btn.setMinimumHeight(30)
        run_btn.clicked.connect(self._run_compare)
        ctrl.addWidget(run_btn)
        ctrl.addStretch(); root.addLayout(ctrl)

        # Delta table
        self._cmp_table = QTableWidget(0, 8)
        self._cmp_table.setHorizontalHeaderLabels([
            "AEP",
            "Crit Dur (A)", "Peak A (m3/s)",
            "Crit Dur (B)", "Peak B (m3/s)",
            "Δ (m3/s)", "Δ (%)", "Critical?"])
        self._cmp_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._cmp_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._cmp_table.setAlternatingRowColors(True)
        self._cmp_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._cmp_table.setMaximumHeight(220)
        self._cmp_table.selectionModel().selectionChanged.connect(
            self._on_cmp_row_selected)
        root.addWidget(self._cmp_table)

        # Overlay plot
        if HAS_MPL:
            self._cmp_fig = Figure(figsize=(8, 3.5), tight_layout=True)
            self._cmp_ax  = self._cmp_fig.add_subplot(111)
            self._cmp_canvas = FigureCanvas(self._cmp_fig)
            root.addWidget(self._cmp_canvas)
        else:
            self._cmp_ax = self._cmp_canvas = None

        self._cmp_rows = []   # list of (aep, crit_a, crit_b)
        return w

    def _run_compare(self):
        name_a = self._cmp_a.currentText()
        name_b = self._cmp_b.currentText()
        node   = self._cmp_node.currentText() or None
        if not name_a or not name_b:
            QMessageBox.warning(self, "Compare", "Select two scenarios."); return
        if name_a == name_b:
            QMessageBox.warning(self, "Compare", "Select two different scenarios."); return

        aeps_a = self._all_aeps(name_a)
        aeps_b = self._all_aeps(name_b)
        aeps   = sorted(set(aeps_a) & set(aeps_b), key=_aep_sort_key)

        self._cmp_rows = []
        self._cmp_table.setRowCount(0)

        for aep in aeps:
            crit_a = self._compute_critical(aep, node, name_a)
            crit_b = self._compute_critical(aep, node, name_b)
            if not crit_a or not crit_b:
                continue
            self._cmp_rows.append((aep, crit_a, crit_b, node))
            pk_a = crit_a['rep_peak']; pk_b = crit_b['rep_peak']
            delta = pk_b - pk_a
            pct   = (delta / pk_a * 100) if pk_a else 0.0
            crit_changed = crit_a['crit_dur'] != crit_b['crit_dur']

            row = self._cmp_table.rowCount()
            self._cmp_table.insertRow(row)
            self._cmp_table.setItem(row, 0, QTableWidgetItem(aep))
            self._cmp_table.setItem(row, 1, QTableWidgetItem(crit_a['crit_dur']))
            pk_a_it = QTableWidgetItem(f"{pk_a:.3f}")
            pk_a_it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._cmp_table.setItem(row, 2, pk_a_it)
            self._cmp_table.setItem(row, 3, QTableWidgetItem(crit_b['crit_dur']))
            pk_b_it = QTableWidgetItem(f"{pk_b:.3f}")
            pk_b_it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._cmp_table.setItem(row, 4, pk_b_it)
            d_it = QTableWidgetItem(f"{delta:+.3f}")
            d_it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            d_it.setForeground(QColor('#dc2626' if delta > 0 else '#16a34a'))
            self._cmp_table.setItem(row, 5, d_it)
            p_it = QTableWidgetItem(f"{pct:+.1f}%")
            p_it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            p_it.setForeground(QColor('#dc2626' if pct > 0 else '#16a34a'))
            self._cmp_table.setItem(row, 6, p_it)
            cc_it = QTableWidgetItem("Yes" if crit_changed else "—")
            cc_it.setTextAlignment(Qt.AlignCenter)
            if crit_changed:
                cc_it.setForeground(QColor('#ea580c'))
            self._cmp_table.setItem(row, 7, cc_it)

    def _on_cmp_row_selected(self):
        if not HAS_MPL or not self._cmp_rows: return
        rows = self._cmp_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._cmp_rows): return
        aep, crit_a, crit_b, node = self._cmp_rows[rows[0].row()]
        name_a = self._cmp_a.currentText()
        name_b = self._cmp_b.currentText()
        col_a  = _SCENARIO_COLORS[0]; col_b = _SCENARIO_COLORS[1]

        self._cmp_ax.clear()
        for crit, col, name, ls in (
                (crit_a, col_a, name_a, '-'),
                (crit_b, col_b, name_b, '--')):
            q = self._get_hydro(crit['rep_entry'], node)
            t = crit['rep_entry'].get('time', [])[:len(q)] if q is not None else []
            if q is None: continue
            self._cmp_ax.plot(t, q, color=col, linewidth=2, linestyle=ls,
                              label=f"{name}  TP{crit['rep_tp']}  "
                                    f"({crit['rep_peak']:.3f} m³/s)")

        self._cmp_ax.set_xlabel("Time (hr)"); self._cmp_ax.set_ylabel("Flow (m³/s)")
        self._cmp_ax.set_title(f"{aep}  |  Critical duration comparison", fontsize=10)
        self._cmp_ax.grid(True, alpha=0.25)
        self._cmp_ax.legend(fontsize=8)
        self._cmp_canvas.draw()

    # ── Tab 5: Export ─────────────────────────────────────────────────────────

    def _tab_export(self):
        w = QWidget(); lay = QVBoxLayout(w)
        box = QGroupBox("Export Settings"); form = QFormLayout(box)
        self._exp_node_combo = QComboBox(); self._exp_node_combo.setMinimumWidth(240)
        self._exp_node_combo.currentIndexChanged.connect(self._refresh_export_info)
        form.addRow("Node (print point):", self._exp_node_combo)
        folder_row = QHBoxLayout()
        self._exp_folder_edit = QLineEdit(); self._exp_folder_edit.setReadOnly(True)
        self._exp_folder_edit.setPlaceholderText("Browse to output folder …")
        folder_btn = QPushButton("Browse…"); folder_btn.setFixedWidth(80)
        folder_btn.clicked.connect(self._browse_export_folder)
        folder_row.addWidget(self._exp_folder_edit); folder_row.addWidget(folder_btn)
        form.addRow("Save folder:", folder_row)
        lay.addWidget(box)
        self._exp_info = QLabel("Select a scenario and node.")
        self._exp_info.setWordWrap(True); self._exp_info.setFont(QFont("Courier New", 8))
        self._exp_info.setStyleSheet("background:#f8f9fa;padding:10px;border:1px solid #dee2e6;")
        lay.addWidget(self._exp_info)
        hint = QLabel("Exports critical duration rep-TP hydrograph and hyetograph "
                      "for every AEP in the active scenario.")
        hint.setWordWrap(True); hint.setStyleSheet("color:gray;font-size:8pt;")
        lay.addWidget(hint)
        brow = QHBoxLayout()
        hydro_btn = QPushButton("Export Hydrographs  (one CSV per AEP)")
        hydro_btn.setMinimumHeight(36); hydro_btn.clicked.connect(self._export_hydros)
        hyeto_btn = QPushButton("Export Hyetographs  (one CSV per AEP)")
        hyeto_btn.setMinimumHeight(36); hyeto_btn.clicked.connect(self._export_hyetos)
        brow.addWidget(hydro_btn); brow.addWidget(hyeto_btn); brow.addStretch()
        lay.addLayout(brow); lay.addStretch()
        return w

    def _browse_export_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder", "")
        if folder: self._exp_folder_edit.setText(folder)

    # ── Scenario management ───────────────────────────────────────────────────

    def _add_scenario(self):
        dlg = _AddScenarioDialog(self)
        if dlg.exec_() != QDialog.Accepted: return
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

    def _remove_scenario(self):
        name = self._scen_combo.currentText()
        if not name: return
        if QMessageBox.question(self, "Remove", f"Remove scenario '{name}'?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        del self._scenarios[name]
        idx = self._scen_combo.findText(name)
        self._scen_combo.removeItem(idx)
        self._refresh_all()

    def _scan_scenario(self, name, folder):
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

        for combo in (self._crit_node_combo, self._exp_node_combo):
            combo.blockSignals(True); combo.clear()
            for n in all_nodes: combo.addItem(n)
            combo.blockSignals(False)

        if aeps:
            self._populate_critical_table()
            self._refresh_viewer()
            self._refresh_export_info()

    def _refresh_compare_combos(self):
        names = list(self._scenarios.keys())
        for combo in (self._cmp_a, self._cmp_b):
            cur = combo.currentText()
            combo.blockSignals(True); combo.clear()
            for n in names: combo.addItem(n)
            if cur in names: combo.setCurrentText(cur)
            combo.blockSignals(False)
        # node combo for compare
        all_nodes = []
        for files in self._scenarios.values():
            for e in files.values():
                for n in e.get('nodes', {}):
                    if n not in all_nodes: all_nodes.append(n)
        self._cmp_node.blockSignals(True); self._cmp_node.clear()
        for n in all_nodes: self._cmp_node.addItem(n)
        self._cmp_node.blockSignals(False)
        # default A/B to first two scenarios
        if len(names) >= 2:
            self._cmp_a.setCurrentIndex(0)
            self._cmp_b.setCurrentIndex(1)

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
        if crit:
            self._peak_summary.setText(
                f"Critical: {crit['crit_dur']}\n"
                f"Mean pk:  {crit['mean_peak']:.3f} m³/s\n"
                f"Rep TP:   TP{crit['rep_tp']}  ({crit['rep_peak']:.3f} m³/s)")

    # ── Export ────────────────────────────────────────────────────────────────

    def _refresh_export_info(self):
        node = self._exp_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps: self._exp_info.setText("No data loaded."); return
        lines = [f"Active scenario: {self._active or '—'}", ""]
        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit: continue
            lines.append(
                f"  {aep:<14}  crit: {crit['crit_dur']:<12}  "
                f"rep TP{crit['rep_tp']}  "
                f"mean pk: {crit['mean_peak']:.3f}  "
                f"rep pk: {crit['rep_peak']:.3f} m³/s")
        self._exp_info.setText("\n".join(lines))

    def _export_hydros(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first."); return
        node = self._exp_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps: QMessageBox.warning(self, "Export", "No data loaded."); return
        saved, skipped = [], []
        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit: skipped.append(aep); continue
            e = crit['rep_entry']
            q = self._get_hydro(e, node)
            t = e.get('time', [])[:len(q)] if q is not None else []
            if q is None: skipped.append(aep); continue
            raw  = os.path.splitext(e['fname'])[0]
            m    = re.search(r'aep\d', raw, re.IGNORECASE)
            stem = raw[m.start():] if m else raw
            fname = os.path.join(folder, f"{stem}_hydro.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Flow (cms)"])
                for tv, qv in zip(t, q):
                    w.writerow([f"{tv:.4f}", f"{float(qv):.6f}"])
            saved.append(os.path.basename(fname))
        msg = f"Exported {len(saved)} hydrograph CSV(s) to:\n{folder}"
        if skipped: msg += f"\n\nSkipped: {', '.join(skipped)}"
        QMessageBox.information(self, "Export Hydrographs", msg)

    def _export_hyetos(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first."); return
        node = self._exp_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps: QMessageBox.warning(self, "Export", "No data loaded."); return
        saved, skipped = [], []
        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit: skipped.append(aep); continue
            e = crit['rep_entry']
            rain_t = e.get('rain_t', []); rain_mm = e.get('rain_mm', [])
            if not rain_mm: skipped.append(aep); continue
            raw  = os.path.splitext(e['fname'])[0]
            m    = re.search(r'aep\d', raw, re.IGNORECASE)
            stem = raw[m.start():] if m else raw
            fname = os.path.join(folder, f"{stem}_rf.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Rainfall (mm)"])
                for tv, rv in zip(rain_t, rain_mm):
                    w.writerow([f"{tv:.4f}", f"{rv:.4f}"])
            saved.append(os.path.basename(fname))
        msg = f"Exported {len(saved)} hyetograph CSV(s) to:\n{folder}"
        if skipped: msg += f"\n\nSkipped (no rainfall data): {', '.join(skipped)}"
        QMessageBox.information(self, "Export Hyetographs", msg)
