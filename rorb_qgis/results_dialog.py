"""
RORB Results Viewer.

Reads a folder of RORBWin .out files (one per AEP / duration / TP),
extracts full hydrographs directly from the 'Hydrograph summary' section,
determines critical duration and representative TP per AEP using the
ARR 2016 Book 4 Ch3 s2.3 mean-TP method, and provides interactive
hydrograph plotting and export.

Expected filename pattern (case-insensitive, flexible prefix):
  {anything}aep{N}_du{dur}(min|hour)tp{M}.out
  e.g.  Tennant_creek_all_ aep1_du1_5hourtp21.out
        run_aep10_du60mintp3.out
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
    QProgressBar, QSplitter,
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


# ── Filename parser ────────────────────────────────────────────────────────────

def _parse_filename(fname):
    """
    Extract (aep_label, dur_label, dur_min, tp_num) from a RORB output filename.

    Handles:
      aep1       → "1% AEP"
      aep0_5     → "0.5% AEP"
      du10min    → "10 min",  10
      du1_5hour  → "1.5 hr",  90
      du72hour   → "72 hr",   4320
      tp21       → 21
    Returns None if any component cannot be found.
    """
    stem = os.path.splitext(fname)[0].lower()

    # AEP: aep{integer} or aep{int}_{frac}
    aep_label = None
    m = re.search(r'aep(\d+)(?:_(\d+))?', stem)
    if m:
        whole = int(m.group(1))
        frac  = m.group(2)
        val   = f"{whole}.{frac}" if frac else str(whole)
        aep_label = f"{val}% AEP"

    # Duration: du{int}[_{frac}](min|hour)
    dur_min, dur_label = None, None
    m = re.search(r'du(\d+)(?:_(\d+))?(min|hour)', stem)
    if m:
        whole  = int(m.group(1))
        frac   = m.group(2)
        unit   = m.group(3)
        val    = float(f"{whole}.{frac}") if frac else float(whole)
        if unit == 'min':
            dur_min   = int(val)
            dur_label = f"{int(val)} min"
        else:
            dur_min   = int(val * 60)
            dur_label = f"{val:g} hr"

    # TP number: tp{N}  (must follow the duration token)
    tp_num = None
    m = re.search(r'tp(\d+)', stem)
    if m:
        tp_num = int(m.group(1))

    if aep_label and dur_min is not None and tp_num is not None:
        return aep_label, dur_label, dur_min, tp_num
    return None


def _aep_sort_key(aep_label):
    """Numeric sort key — higher % (more frequent) first."""
    m = re.search(r'([\d.]+)', aep_label)
    return -float(m.group(1)) if m else 0.0


# ── Background scanner ─────────────────────────────────────────────────────────

class _ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    result   = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, folder):
        super().__init__()
        self.folder = folder

    def run(self):
        try:
            from .core.engine import parse_out_hydrograph, parse_out_rainfall
            fnames = sorted(
                f for f in os.listdir(self.folder)
                if f.lower().endswith('.out')
                and not f.lower().startswith('rorb'))   # skip RORBWin.out etc.
            out = {}
            for i, fname in enumerate(fnames):
                self.progress.emit(i + 1, len(fnames), fname)
                path   = os.path.join(self.folder, fname)
                parsed = _parse_filename(fname)
                try:
                    nodes, time_axis, dt = parse_out_hydrograph(path)
                except Exception:
                    nodes, time_axis, dt = {}, [], None
                try:
                    rain_t, rain_mm, _ = parse_out_rainfall(path)
                except Exception:
                    rain_t, rain_mm = [], []
                out[path] = {
                    'fname':   fname,
                    'path':    path,
                    'parsed':  parsed,
                    'nodes':   nodes,
                    'time':    time_axis,
                    'dt':      dt,
                    'rain_t':  rain_t,
                    'rain_mm': rain_mm,
                }
            self.result.emit(out)
        except Exception:
            self.error.emit(traceback.format_exc())


# ── Main dialog ────────────────────────────────────────────────────────────────

class RorbResultsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RORB Results Viewer")
        self.setMinimumSize(1120, 780)
        self._files        = {}
        self._worker       = None
        self._tp_rows      = {}   # tp_num -> (QCheckBox, color_str)
        self._crit_rows    = []   # list of crit dicts, one per AEP
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Folder row (always visible)
        frow = QHBoxLayout()
        frow.addWidget(QLabel("RORB output folder:"))
        self._folder_edit = QLineEdit()
        self._folder_edit.setReadOnly(True)
        self._folder_edit.setPlaceholderText(
            "Browse to folder containing RORBWin .out files …")
        browse_btn = QPushButton("Browse…"); browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_folder)
        self._scan_btn = QPushButton("Scan"); self._scan_btn.setFixedWidth(60)
        self._scan_btn.setEnabled(False)
        self._scan_btn.clicked.connect(self._scan_folder)
        self._scan_btn.setStyleSheet(
            "QPushButton { background:#16a34a; color:white; font-weight:bold; border-radius:3px; }"
            "QPushButton:disabled { background:#9ca3af; }"
            "QPushButton:hover { background:#15803d; }")
        frow.addWidget(self._folder_edit)
        frow.addWidget(browse_btn)
        frow.addWidget(self._scan_btn)
        root.addLayout(frow)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_files(),    "Files")
        self._tabs.addTab(self._tab_critical(), "Critical Events")
        self._tabs.addTab(self._tab_viewer(),   "Hydrograph Viewer")
        self._tabs.addTab(self._tab_export(),   "Export")
        root.addWidget(self._tabs)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(QPushButton("Close", clicked=self.close))
        root.addLayout(btn_row)

    # ── Tab 1: Files ─────────────────────────────────────────────────────────

    def _tab_files(self):
        w = QWidget(); lay = QVBoxLayout(w)

        self._scan_status   = QLabel("No folder selected.")
        self._scan_status.setStyleSheet("color:gray;font-size:9pt;")
        self._scan_progress = QProgressBar()
        self._scan_progress.setVisible(False)
        lay.addWidget(self._scan_status)
        lay.addWidget(self._scan_progress)

        self._file_table = QTableWidget(0, 5)
        self._file_table.setHorizontalHeaderLabels(
            ["Filename", "AEP", "Duration", "TP", "Status"])
        self._file_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._file_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._file_table.setAlternatingRowColors(True)
        lay.addWidget(self._file_table)
        return w

    # ── Tab 2: Critical Events ────────────────────────────────────────────────

    def _tab_critical(self):
        w = QWidget(); lay = QVBoxLayout(w)

        # Header row
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel(
            "Critical Events  (ARR 2016: mean of TPs  →  critical duration  →  rep TP):"))
        hdr.addStretch()

        # Node selector
        hdr.addWidget(QLabel("Node:"))
        self._crit_node_combo = QComboBox(); self._crit_node_combo.setMinimumWidth(160)
        self._crit_node_combo.currentIndexChanged.connect(self._populate_critical_table)
        hdr.addWidget(self._crit_node_combo)

        exp_btn = QPushButton("Export Critical CSV…")
        exp_btn.clicked.connect(self._export_critical_csv)
        hdr.addWidget(exp_btn)
        lay.addLayout(hdr)

        # Summary table
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

        # Plot
        if HAS_MPL:
            self._crit_fig    = Figure(figsize=(8, 3.5), tight_layout=True)
            self._crit_ax     = self._crit_fig.add_subplot(111)
            self._crit_canvas = FigureCanvas(self._crit_fig)
            lay.addWidget(self._crit_canvas)
        else:
            self._crit_ax = self._crit_canvas = None
            lay.addWidget(QLabel("Install matplotlib to see plots."))

        return w

    def _populate_critical_table(self):
        self._crit_rows = []
        self._crit_table.setRowCount(0)

        node = self._crit_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps:
            return

        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit:
                continue

            # time to peak from rep TP hydrograph
            rep_e = crit['rep_entry']
            q     = self._get_hydro(rep_e, node)
            t     = rep_e.get('time', [])
            ttp   = 0.0
            if q is not None and len(q):
                pk_idx = int(np.argmax(q))
                ttp = t[pk_idx] if pk_idx < len(t) else 0.0

            crit['ttp']  = ttp
            crit['aep']  = aep
            crit['node'] = node
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
        if not HAS_MPL:
            return
        rows = self._crit_table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if idx >= len(self._crit_rows):
            return
        crit = self._crit_rows[idx]
        node = crit.get('node')

        rep_e = crit['rep_entry']
        q     = self._get_hydro(rep_e, node)
        t     = rep_e.get('time', [])[:len(q)] if q is not None else []
        if q is None:
            return

        self._crit_ax.clear()

        # Plot rep TP
        self._crit_ax.plot(t, q, color='steelblue', linewidth=2,
                           label=f"Rep TP{crit['rep_tp']}  "
                                 f"({crit['rep_peak']:.3f} m³/s)")
        # Mean peak line
        self._crit_ax.axhline(
            crit['mean_peak'], color='#111827', linewidth=1.2,
            linestyle='--',
            label=f"Mean of {crit['n_tps']} TPs  "
                  f"({crit['mean_peak']:.3f} m³/s)")

        self._crit_ax.set_xlabel("Time (hr)")
        self._crit_ax.set_ylabel("Flow (m³/s)")
        self._crit_ax.set_title(
            f"{crit['aep']}  |  Critical duration: {crit['crit_dur']}  |  "
            f"Rep TP{crit['rep_tp']}  |  {node or 'outlet'}",
            fontsize=9)
        self._crit_ax.grid(True, alpha=0.25)
        self._crit_ax.legend(fontsize=8)

        if t:
            pk_t = t[int(np.argmax(q))]
            self._crit_ax.set_xlim(0, min(t[-1], pk_t * 3 + 2))

        self._crit_canvas.draw()

    def _export_critical_csv(self):
        if not self._crit_rows:
            QMessageBox.warning(self, "Export", "No critical events computed yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Critical Events", "", "CSV (*.csv)")
        if not path:
            return
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

        self._mean_chk = QCheckBox("Mean peak line")
        self._mean_chk.setChecked(True)
        self._mean_chk.stateChanged.connect(self._replot)
        ctrl.addWidget(self._mean_chk)

        self._hilite_chk = QCheckBox("Highlight rep TP")
        self._hilite_chk.setChecked(True)
        self._hilite_chk.stateChanged.connect(self._replot)
        ctrl.addWidget(self._hilite_chk)

        ctrl.addStretch()
        root.addLayout(ctrl)

        splitter = QSplitter(Qt.Horizontal)

        # Left: TP toggles + summary
        left = QWidget(); llay = QVBoxLayout(left)
        llay.setContentsMargins(4, 4, 4, 4)
        llay.addWidget(QLabel("Temporal Patterns:"))

        tbtn = QHBoxLayout()
        all_b  = QPushButton("All");  all_b.setFixedWidth(38)
        none_b = QPushButton("None"); none_b.setFixedWidth(44)
        all_b.clicked.connect(lambda: self._toggle_all_tps(True))
        none_b.clicked.connect(lambda: self._toggle_all_tps(False))
        tbtn.addWidget(all_b); tbtn.addWidget(none_b); tbtn.addStretch()
        llay.addLayout(tbtn)

        self._tp_scroll = QScrollArea(); self._tp_scroll.setWidgetResizable(True)
        self._tp_inner  = QWidget()
        self._tp_vbox   = QVBoxLayout(self._tp_inner)
        self._tp_vbox.setSpacing(2)
        self._tp_vbox.addStretch()
        self._tp_scroll.setWidget(self._tp_inner)
        llay.addWidget(self._tp_scroll)

        self._peak_summary = QLabel("")
        self._peak_summary.setWordWrap(True)
        self._peak_summary.setFont(QFont("Courier New", 8))
        self._peak_summary.setStyleSheet(
            "color:#1e293b;background:#f8fafc;padding:6px;"
            "border:1px solid #cbd5e1;")
        llay.addWidget(self._peak_summary)

        splitter.addWidget(left)

        if HAS_MPL:
            self._fig    = Figure(figsize=(7, 4), tight_layout=True)
            self._ax     = self._fig.add_subplot(111)
            self._canvas = FigureCanvas(self._fig)
            splitter.addWidget(self._canvas)
        else:
            splitter.addWidget(QLabel("Install matplotlib to see plots."))

        splitter.setSizes([170, 900])
        root.addWidget(splitter)
        return w

    # ── Tab 4: Export ─────────────────────────────────────────────────────────

    def _tab_export(self):
        w = QWidget(); lay = QVBoxLayout(w)

        # Settings
        box = QGroupBox("Export Settings")
        form = QFormLayout(box)

        self._exp_node_combo = QComboBox(); self._exp_node_combo.setMinimumWidth(240)
        self._exp_node_combo.currentIndexChanged.connect(self._refresh_export_info)
        form.addRow("Node (print point):", self._exp_node_combo)

        folder_row = QHBoxLayout()
        self._exp_folder_edit = QLineEdit()
        self._exp_folder_edit.setReadOnly(True)
        self._exp_folder_edit.setPlaceholderText("Browse to output folder …")
        folder_btn = QPushButton("Browse…"); folder_btn.setFixedWidth(80)
        folder_btn.clicked.connect(self._browse_export_folder)
        folder_row.addWidget(self._exp_folder_edit)
        folder_row.addWidget(folder_btn)
        form.addRow("Save folder:", folder_row)
        lay.addWidget(box)

        # Info panel
        self._exp_info = QLabel("Scan a folder and select a node to see the critical summary.")
        self._exp_info.setWordWrap(True)
        self._exp_info.setFont(QFont("Courier New", 8))
        self._exp_info.setStyleSheet(
            "background:#f8f9fa;padding:10px;border:1px solid #dee2e6;")
        lay.addWidget(self._exp_info)

        hint = QLabel(
            "Exports one CSV per AEP using the critical duration and representative TP "
            "(ARR 2016 Book 4 Ch3 s2.3).  Files are named  {AEP}_critical_hydrograph.csv  "
            "and  {AEP}_critical_hyetograph.csv.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:gray;font-size:8pt;")
        lay.addWidget(hint)

        brow = QHBoxLayout()
        hydro_btn = QPushButton("Export Hydrographs  (one CSV per AEP)")
        hydro_btn.setMinimumHeight(36)
        hydro_btn.clicked.connect(self._export_hydros)
        hyeto_btn = QPushButton("Export Hyetographs  (one CSV per AEP)")
        hyeto_btn.setMinimumHeight(36)
        hyeto_btn.clicked.connect(self._export_hyetos)
        brow.addWidget(hydro_btn)
        brow.addWidget(hyeto_btn)
        brow.addStretch()
        lay.addLayout(brow)
        lay.addStretch()
        return w

    def _browse_export_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder", "")
        if folder:
            self._exp_folder_edit.setText(folder)

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder of RORBWin .out files", "")
        if not folder:
            return
        self._folder_edit.setText(folder)
        self._scan_btn.setEnabled(True)
        self._scan_status.setText(f"Ready — {folder}")

    def _scan_folder(self):
        folder = self._folder_edit.text()
        if not folder or not os.path.isdir(folder):
            return
        self._scan_progress.setVisible(True)
        self._scan_progress.setValue(0)
        self._scan_btn.setEnabled(False)
        self._scan_status.setText("Scanning …")
        self._file_table.setRowCount(0)
        self._worker = _ScanWorker(folder)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.result.connect(self._on_scan_done)
        self._worker.error.connect(self._on_scan_error)
        self._worker.start()

    def _on_scan_progress(self, cur, total, fname):
        self._scan_progress.setMaximum(total)
        self._scan_progress.setValue(cur)
        self._scan_status.setText(f"Reading {cur}/{total}: {fname}")

    def _on_scan_done(self, files):
        self._files = files
        self._scan_progress.setVisible(False)
        self._scan_btn.setEnabled(True)

        ok   = sum(1 for e in files.values() if e.get('parsed') and e.get('nodes'))
        fail = len(files) - ok
        self._scan_status.setText(
            f"{len(files)} .out files  —  {ok} parsed OK"
            + (f"  ({fail} unrecognised)" if fail else ""))

        self._file_table.setRowCount(0)
        for e in sorted(files.values(), key=lambda x: x['fname']):
            row = self._file_table.rowCount()
            self._file_table.insertRow(row)
            self._file_table.setItem(row, 0, QTableWidgetItem(e['fname']))
            p = e.get('parsed')
            if p:
                aep, dur_label, _, tp = p
                self._file_table.setItem(row, 1, QTableWidgetItem(aep))
                self._file_table.setItem(row, 2, QTableWidgetItem(dur_label))
                tpi = QTableWidgetItem(str(tp)); tpi.setTextAlignment(Qt.AlignCenter)
                self._file_table.setItem(row, 3, tpi)
                has_nodes = bool(e.get('nodes'))
                si = QTableWidgetItem("OK" if has_nodes else "No hydrograph")
                si.setForeground(QColor('#16a34a' if has_nodes else '#dc2626'))
                self._file_table.setItem(row, 4, si)
            else:
                for c, txt in enumerate(["-", "-", "-", "Name not parsed"], 1):
                    it = QTableWidgetItem(txt)
                    it.setForeground(QColor('#f97316'))
                    self._file_table.setItem(row, c, it)

        self._populate_combos()

    def _on_scan_error(self, msg):
        self._scan_progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._scan_status.setText("Error during scan.")
        QMessageBox.critical(self, "Scan error", msg)

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _valid_entries(self):
        return [e for e in self._files.values()
                if e.get('parsed') and e.get('nodes')]

    def _entries_for_aep(self, aep):
        return [e for e in self._valid_entries() if e['parsed'][0] == aep]

    def _all_aeps(self):
        return sorted(
            set(e['parsed'][0] for e in self._valid_entries()),
            key=_aep_sort_key)

    def _get_hydro(self, entry, node):
        """Return flow array for the requested node, falling back to last node."""
        nodes_d = entry.get('nodes', {})
        if not nodes_d:
            return None
        if node and node in nodes_d:
            return nodes_d[node]
        return list(nodes_d.values())[-1]

    # ── Critical duration / rep TP ────────────────────────────────────────────

    def _compute_critical(self, aep, node):
        """
        ARR 2016 Book 4 Ch3 s2.3:
        1. Group runs by duration.
        2. Mean peak per duration (across TPs).
        3. Critical duration = duration with highest mean peak.
        4. Rep TP = TP whose peak is closest to that mean.
        """
        entries = self._entries_for_aep(aep)
        if not entries:
            return None

        dur_groups = defaultdict(list)
        for e in entries:
            _, dur_label, dur_min, tp = e['parsed']
            q    = self._get_hydro(e, node)
            peak = float(np.max(q)) if q is not None and len(q) else 0.0
            dur_groups[(dur_label, dur_min)].append((tp, peak, e))

        if not dur_groups:
            return None

        dur_means = {
            key: (float(np.mean([pk for _, pk, _ in tps])), tps)
            for key, tps in dur_groups.items()
        }

        (crit_lbl, crit_min), (mean_peak, crit_tps) = max(
            dur_means.items(), key=lambda x: x[1][0])

        rep_tp_num, rep_peak, rep_entry = min(
            crit_tps, key=lambda x: abs(x[1] - mean_peak))

        return {
            'crit_dur':  crit_lbl,
            'crit_min':  crit_min,
            'rep_tp':    rep_tp_num,
            'rep_peak':  rep_peak,
            'mean_peak': mean_peak,
            'n_tps':     len(crit_tps),
            'rep_entry': rep_entry,
            'crit_tps':  crit_tps,
            'dur_means': {
                lbl: v
                for (lbl, _), (v, _) in dur_means.items()
            },
        }

    # ── Combo population ──────────────────────────────────────────────────────

    def _populate_combos(self):
        aeps = self._all_aeps()

        # Collect all node names across all valid entries
        all_nodes = []
        for e in self._valid_entries():
            for n in e['nodes']:
                if n not in all_nodes:
                    all_nodes.append(n)

        self._aep_combo.blockSignals(True)
        self._aep_combo.clear()
        for a in aeps:
            self._aep_combo.addItem(a)
        self._aep_combo.blockSignals(False)

        for combo in (self._crit_node_combo, self._exp_node_combo):
            combo.blockSignals(True)
            combo.clear()
            for n in all_nodes:
                combo.addItem(n)
            combo.blockSignals(False)

        if aeps:
            self._populate_critical_table()
            self._refresh_viewer()
            self._refresh_export_info()

    # ── Viewer ────────────────────────────────────────────────────────────────

    def _refresh_viewer(self):
        aep = self._aep_combo.currentText()
        if not aep:
            return
        entries = self._entries_for_aep(aep)
        if not entries:
            return

        # Populate node combo
        all_nodes = []
        for e in entries:
            for n in e['nodes']:
                if n not in all_nodes:
                    all_nodes.append(n)
        self._node_combo_v.blockSignals(True)
        self._node_combo_v.clear()
        for n in all_nodes:
            self._node_combo_v.addItem(n)
        self._node_combo_v.blockSignals(False)

        # Populate duration combo (sorted by duration minutes)
        durs = sorted(
            set((e['parsed'][1], e['parsed'][2]) for e in entries),
            key=lambda x: x[1])
        self._dur_combo.blockSignals(True)
        self._dur_combo.clear()
        self._dur_combo.addItem("Critical (auto)", None)
        for lbl, mins in durs:
            self._dur_combo.addItem(lbl, mins)
        self._dur_combo.blockSignals(False)

        self._rebuild_tp_checks()
        self._replot()

    def _on_dur_changed(self):
        self._rebuild_tp_checks()
        self._replot()

    def _rebuild_tp_checks(self):
        while self._tp_vbox.count():
            item = self._tp_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._tp_rows.clear()

        aep  = self._aep_combo.currentText()
        node = self._node_combo_v.currentText() or None
        if not aep:
            self._tp_vbox.addStretch()
            return

        entries  = self._entries_for_aep(aep)
        dur_min  = self._dur_combo.currentData()

        if dur_min is None:
            crit = self._compute_critical(aep, node)
            if crit:
                dur_min = crit['crit_min']

        if dur_min is not None:
            entries = [e for e in entries if e['parsed'][2] == dur_min]

        for i, e in enumerate(sorted(entries, key=lambda x: x['parsed'][3])):
            tp_num = e['parsed'][3]
            color  = _TP_COLORS[i % len(_TP_COLORS)]

            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(2, 1, 2, 1)

            swatch = QLabel("  ")
            swatch.setFixedSize(12, 16)
            swatch.setStyleSheet(f"background:{color};border:1px solid #555;")

            chk = QCheckBox(f"TP{tp_num}")
            chk.setChecked(True)
            chk.stateChanged.connect(self._replot)

            row_l.addWidget(swatch)
            row_l.addWidget(chk)
            row_l.addStretch()
            self._tp_vbox.addWidget(row_w)
            self._tp_rows[tp_num] = (chk, color)

        self._tp_vbox.addStretch()

    def _toggle_all_tps(self, state):
        for chk, _ in self._tp_rows.values():
            chk.setChecked(state)

    def _replot(self):
        if not HAS_MPL or not self._files:
            return
        aep  = self._aep_combo.currentText()
        node = self._node_combo_v.currentText() or None
        if not aep:
            return

        entries  = self._entries_for_aep(aep)
        dur_min  = self._dur_combo.currentData()
        crit     = self._compute_critical(aep, node)

        if dur_min is None and crit:
            dur_min = crit['crit_min']
        if dur_min is not None:
            entries = [e for e in entries if e['parsed'][2] == dur_min]

        self._ax.clear()

        rep_tp      = crit['rep_tp'] if crit else None
        show_mean   = self._mean_chk.isChecked()
        show_hilite = self._hilite_chk.isChecked()
        active_peaks = []

        for e in sorted(entries, key=lambda x: x['parsed'][3]):
            tp_num = e['parsed'][3]
            chk, color = self._tp_rows.get(tp_num, (None, '#888'))
            if chk and not chk.isChecked():
                continue
            q = self._get_hydro(e, node)
            if q is None:
                continue
            t      = e.get('time', [])[:len(q)]
            pk     = float(np.max(q))
            is_rep = show_hilite and (tp_num == rep_tp)
            lbl    = f"TP{tp_num}" + (" ★" if is_rep else "")
            self._ax.plot(
                t, q,
                color=color,
                linewidth=2.5 if is_rep else 1.2,
                label=lbl,
                alpha=1.0 if is_rep else 0.70,
                zorder=4 if is_rep else 2,
            )
            active_peaks.append(pk)

        if show_mean and active_peaks:
            mean_pk = float(np.mean(active_peaks))
            self._ax.axhline(
                mean_pk, color='#111827', linewidth=1.4,
                linestyle='--', zorder=5,
                label=f"Mean: {mean_pk:.3f} m³/s")

        dur_txt = self._dur_combo.currentText()
        self._ax.set_xlabel("Time (hr)")
        self._ax.set_ylabel("Flow (m³/s)")
        self._ax.set_title(
            f"{aep}  |  {dur_txt}  |  {node or 'outlet'}", fontsize=10)
        self._ax.grid(True, alpha=0.25)
        if self._ax.lines:
            self._ax.legend(fontsize=7.5, loc='upper right', framealpha=0.85)

        # x-limit: ~3× time-to-peak of the combined envelope
        all_q_concat = []
        t_ref = []
        for e in entries:
            q = self._get_hydro(e, node)
            if q is not None:
                all_q_concat.extend(list(q))
                t_ref = e.get('time', [])
        if all_q_concat and t_ref:
            pk_idx = int(np.argmax(all_q_concat[:len(t_ref)]))
            pk_t   = t_ref[pk_idx] if pk_idx < len(t_ref) else 10
            self._ax.set_xlim(0, min(
                t_ref[-1] if t_ref else 100, pk_t * 3 + 2))

        self._canvas.draw()

        # Update left-panel summary
        if crit:
            self._peak_summary.setText(
                f"Critical: {crit['crit_dur']}\n"
                f"Mean pk:  {crit['mean_peak']:.3f} m³/s\n"
                f"Rep TP:   TP{crit['rep_tp']}  ({crit['rep_peak']:.3f} m³/s)")

    # ── Export ────────────────────────────────────────────────────────────────

    def _refresh_export_info(self):
        node = self._exp_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps:
            self._exp_info.setText("No data loaded.")
            return

        lines = ["Critical summary per AEP:", ""]
        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit:
                continue
            lines.append(
                f"  {aep:<14}  crit dur: {crit['crit_dur']:<12}  "
                f"rep TP{crit['rep_tp']}  "
                f"mean pk: {crit['mean_peak']:.3f}  "
                f"rep pk: {crit['rep_peak']:.3f} m³/s")
        self._exp_info.setText("\n".join(lines))

    def _export_hydros(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first.")
            return
        node = self._exp_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps:
            QMessageBox.warning(self, "Export", "No data loaded."); return

        saved, skipped = [], []
        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit:
                skipped.append(aep); continue
            e = crit['rep_entry']
            q = self._get_hydro(e, node)
            t = e.get('time', [])[:len(q)] if q is not None else []
            if q is None:
                skipped.append(aep); continue

            raw   = os.path.splitext(crit['rep_entry']['fname'])[0]
            m     = re.search(r'aep\d', raw, re.IGNORECASE)
            stem  = raw[m.start():] if m else raw
            fname = os.path.join(folder, f"{stem}_hydro.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Flow (cms)"])
                for tv, qv in zip(t, q):
                    w.writerow([f"{tv:.4f}", f"{float(qv):.6f}"])
            saved.append(os.path.basename(fname))

        msg = f"Exported {len(saved)} hydrograph CSV(s) to:\n{folder}"
        if skipped:
            msg += f"\n\nSkipped (no data): {', '.join(skipped)}"
        QMessageBox.information(self, "Export Hydrographs", msg)

    def _export_hyetos(self):
        folder = self._exp_folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Export", "Select an output folder first.")
            return
        node = self._exp_node_combo.currentText() or None
        aeps = self._all_aeps()
        if not aeps:
            QMessageBox.warning(self, "Export", "No data loaded."); return

        saved, skipped = [], []
        for aep in aeps:
            crit = self._compute_critical(aep, node)
            if not crit:
                skipped.append(aep); continue
            e      = crit['rep_entry']
            rain_t  = e.get('rain_t',  [])
            rain_mm = e.get('rain_mm', [])
            if not rain_mm:
                skipped.append(aep); continue

            raw   = os.path.splitext(crit['rep_entry']['fname'])[0]
            m     = re.search(r'aep\d', raw, re.IGNORECASE)
            stem  = raw[m.start():] if m else raw
            fname = os.path.join(folder, f"{stem}_rf.csv")
            with open(fname, 'w', newline='') as f:
                w = csv_mod.writer(f)
                w.writerow(["Time (hr)", "Rainfall (mm)"])
                for tv, rv in zip(rain_t, rain_mm):
                    w.writerow([f"{tv:.4f}", f"{rv:.4f}"])
            saved.append(os.path.basename(fname))

        msg = f"Exported {len(saved)} hyetograph CSV(s) to:\n{folder}"
        if skipped:
            msg += f"\n\nSkipped (no rainfall data): {', '.join(skipped)}"
        QMessageBox.information(self, "Export Hyetographs", msg)
