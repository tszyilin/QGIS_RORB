import os
import traceback
import numpy as np

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QTextEdit, QTabWidget, QWidget,
    QDoubleSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QSplitter, QHeaderView, QFileDialog, QMessageBox,
    QLineEdit, QCheckBox, QScrollArea, QGridLayout,
    QProgressBar, QSpinBox,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QFont, QColor
from qgis.core import QgsMapLayerProxyModel
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox

try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    HAS_MPL = True
except Exception:
    HAS_MPL = False


# ---------------------------------------------------------------------------
class _LayerRow(QHBoxLayout):
    def __init__(self, layer_filter):
        super().__init__()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(layer_filter)
        self.layer_combo.setAllowEmptyLayer(True)
        self.addWidget(self.layer_combo, 3)
        self._fields = {}

    def add_field(self, label, key, allow_empty=False):
        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fc = QgsFieldComboBox()
        fc.setAllowEmptyFieldName(allow_empty)
        self.layer_combo.layerChanged.connect(fc.setLayer)
        fc.setLayer(self.layer_combo.currentLayer())
        self.addWidget(lbl)
        self.addWidget(fc, 2)
        self._fields[key] = fc
        return fc

    def layer(self):
        return self.layer_combo.currentLayer()

    def field(self, key):
        fc = self._fields.get(key)
        return (fc.currentField() or None) if fc else None


# ---------------------------------------------------------------------------
class FilesWorker(QThread):
    log_msg  = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, catg, out, stm, csv_path):
        super().__init__()
        self.catg = catg; self.out = out
        self.stm = stm or None; self.csv_path = csv_path or None

    def run(self):
        try:
            from .core.engine import run_from_files, parse_rorb_csv
            self.log_msg.emit(f"Parsing .catg: {self.catg}")
            self.log_msg.emit(f"Parsing .out:  {self.out}")
            if self.stm:
                self.log_msg.emit(f"Parsing .stm:  {self.stm}")
            res = run_from_files(self.catg, self.out, self.stm)
            self.log_msg.emit(
                f"kc={res['kc']}  m={res['m']}  "
                f"IL={res['il']} mm  CL={res['cl']} mm/hr  dt={res['dt']} hr")
            rorb_nodes, rorb_time = {}, []
            if self.csv_path and os.path.exists(self.csv_path):
                self.log_msg.emit(f"RORB CSV: {self.csv_path}")
                rorb_nodes, rorb_time, _ = parse_rorb_csv(self.csv_path)
            res['rorb_nodes'] = rorb_nodes
            res['rorb_time']  = rorb_time
            res['source']     = 'files'
            for name, q in res['hydros'].items():
                rp = res['rorb_peaks'].get(name)
                ep = float(np.max(q))
                ds = f"  RORB={rp:.3f} diff={((ep-rp)/rp*100):+.1f}%" if rp else ""
                self.log_msg.emit(f"  {name}: peak={ep:.3f} m3/s{ds}")
            self.finished.emit(res)
        except Exception:
            self.error.emit(traceback.format_exc())


# ---------------------------------------------------------------------------
class BatchWorker(QThread):
    progress       = pyqtSignal(int, int, str)
    partial_result = pyqtSignal(dict)
    log_msg        = pyqtSignal(str)
    finished       = pyqtSignal()
    error          = pyqtSignal(str)

    def __init__(self, catch_params, model_params, combos,
                 ifd_rows, tp_patterns, arf):
        super().__init__()
        self.catch_params = catch_params
        self.model_params = model_params
        self.combos       = combos
        self.ifd_rows     = ifd_rows
        self.tp_patterns  = tp_patterns
        self.arf          = arf

    def run(self):
        try:
            from .core.qgis_builder import build_reaches, build_confluences, build_basins
            from .core.catchment import Catchment
            from .core.simulation import run as sim_run
            from .core.storm import (get_ifd_depth, get_temporal_pattern,
                                      aep_to_band, build_rainfall_series)
            cp = self.catch_params
            mp = self.model_params

            self.log_msg.emit("Building catchment...")
            reaches     = build_reaches(cp['reach_lyr'], cp['fld_rid'],
                                        cp['fld_slope'], cp['fld_type'])
            confluences = build_confluences(cp['junc_lyr'], cp['fld_jid'],
                                            cp['fld_out'])
            basins      = build_basins(cp['cent_lyr'], cp['basin_lyr'],
                                       cp['fld_cid'], cp['fld_fi'])
            catchment   = Catchment(confluences, basins, reaches)
            catchment.connect()
            self.log_msg.emit(
                f"  {len(reaches)} reaches  {len(confluences)} confluences  "
                f"{len(basins)} sub-areas")

            total = len(self.combos)
            for i, (aep, dur_label, dur_min, tp_num) in enumerate(self.combos):
                label = f"{aep} / {dur_label} / TP{tp_num}"
                self.progress.emit(i + 1, total, label)
                band  = aep_to_band(aep)
                depth = get_ifd_depth(self.ifd_rows, aep, dur_min)
                if depth is None:
                    self.log_msg.emit(f"  Skip {label}: no IFD depth")
                    continue
                incs, dt_min = get_temporal_pattern(
                    self.tp_patterns, dur_min, band, tp_num)
                if not incs:
                    self.log_msg.emit(f"  Skip {label}: no temporal pattern")
                    continue
                rain_ts = build_rainfall_series(depth, self.arf, incs)
                dt_hr   = dt_min / 60.0
                n_storm = len(rain_ts)
                results = sim_run(
                    catchment   = catchment,
                    kc          = mp['kc'],
                    m           = mp['m'],
                    dt_hr       = dt_hr,
                    rainfall_mm = np.array(rain_ts),
                    loss_model  = 'il_cl',
                    il_mm       = mp['il'],
                    cl_mm_hr    = mp['cl'],
                    n_steps     = max(n_storm * 4, 200),
                )
                outlet = next((r for r in results.values() if r['is_outlet']), None)
                self.partial_result.emit({
                    'aep':       aep,
                    'dur_label': dur_label,
                    'dur_min':   dur_min,
                    'tp_num':    tp_num,
                    'peak':      float(outlet['peak_flow'])    if outlet else 0.0,
                    'ttp':       float(outlet['time_to_peak']) if outlet else 0.0,
                    'hydro':     outlet['hydro'].tolist()      if outlet else [],
                    'time':      outlet['time'].tolist()       if outlet else [],
                    'depth_mm':  round(depth * self.arf, 2),
                    'dt_hr':     dt_hr,
                })
            self.finished.emit()
        except Exception:
            self.error.emit(traceback.format_exc())


# ---------------------------------------------------------------------------
class RorbModelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RORB Builder")
        self.setMinimumSize(1050, 740)
        self._results       = None
        self._worker        = None
        self._batch_rows    = []
        self._critical_rows = []
        self._storm_data    = {}
        self._aep_chks      = {}
        self._dur_chks      = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_catchment(),  "Catchment")
        self._tabs.addTab(self._tab_network(),    "Network")
        self._tabs.addTab(self._tab_inputs(),     "Inputs")
        self._tabs.addTab(self._tab_parameters(), "Parameters")
        self._tabs.addTab(self._tab_run(),        "Run")
        self._tabs.addTab(self._tab_files(),      "Files")
        self._tabs.addTab(self._tab_results(),    "Results")
        root.addWidget(self._tabs)

        btn_row = QHBoxLayout()
        self._run_files_btn = QPushButton("Run from Files")
        self._run_files_btn.setMinimumHeight(36)
        self._run_files_btn.clicked.connect(self._on_run_files)
        catg_btn  = QPushButton("Export .catg...")
        catg_btn.setMinimumHeight(36)
        catg_btn.clicked.connect(self._on_export_catg)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addStretch()
        btn_row.addWidget(self._run_files_btn)
        btn_row.addWidget(catg_btn)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ---- Catchment --------------------------------------------------------
    def _tab_catchment(self):
        w = QWidget(); layout = QVBoxLayout(w)
        box = QGroupBox("Input Layers & Field Mapping")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._reach_row = _LayerRow(QgsMapLayerProxyModel.LineLayer)
        self._reach_row.add_field("id:", "id")
        self._reach_row.add_field("type(1-4):", "type", allow_empty=True)
        self._reach_row.add_field("slope:", "slope", allow_empty=True)
        form.addRow("Reaches:", self._reach_row)

        self._cent_row = _LayerRow(QgsMapLayerProxyModel.PointLayer)
        self._cent_row.add_field("id:", "id")
        self._cent_row.add_field("fi [0-1]:", "fi", allow_empty=True)
        form.addRow("Centroids:", self._cent_row)

        self._junc_row = _LayerRow(QgsMapLayerProxyModel.PointLayer)
        self._junc_row.add_field("id:", "id")
        self._junc_row.add_field("outlet(0/1):", "out")
        form.addRow("Confluences:", self._junc_row)

        self._basin_row = _LayerRow(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow("Sub-catchments:", self._basin_row)
        layout.addWidget(box)

        note = QLabel("Reach type: 1=Natural  2=Unlined  3=Lined  4=Drowned")
        note.setStyleSheet("color: gray; font-size: 8pt;")
        layout.addWidget(note)

        snap_row = QHBoxLayout()
        snap_row.addWidget(QLabel("Snap tolerance:"))
        self._snap_tol = QDoubleSpinBox()
        self._snap_tol.setRange(0.0, 1000.0); self._snap_tol.setValue(1.0)
        self._snap_tol.setDecimals(2); self._snap_tol.setSuffix("  m")
        snap_row.addWidget(self._snap_tol); snap_row.addStretch()
        layout.addLayout(snap_row)
        layout.addStretch()
        return w

    # ---- Network ----------------------------------------------------------
    def _tab_network(self):
        w = QWidget(); layout = QVBoxLayout(w)
        top = QHBoxLayout()
        self._net_reaches_lbl   = QLabel("Reaches: -")
        self._net_junctions_lbl = QLabel("Confluences: -")
        self._net_basins_lbl    = QLabel("Sub-areas: -")
        self._net_length_lbl    = QLabel("Total: - km")
        chip = ("padding:3px 8px;background:#e8f0fe;"
                "border:1px solid #aac;border-radius:4px;font-size:9pt;")
        for lbl in (self._net_reaches_lbl, self._net_junctions_lbl,
                    self._net_basins_lbl, self._net_length_lbl):
            lbl.setStyleSheet(chip); top.addWidget(lbl)
        top.addStretch()
        build_btn = QPushButton("Build Network")
        build_btn.clicked.connect(self._build_network_table)
        top.addWidget(build_btn)
        exp_btn = QPushButton("Export CSV...")
        exp_btn.clicked.connect(self._export_network_csv)
        top.addWidget(exp_btn)
        layout.addLayout(top)
        self._net_warn = QLabel("")
        self._net_warn.setWordWrap(True)
        self._net_warn.setStyleSheet("color:#b94a00;font-size:8pt;")
        layout.addWidget(self._net_warn)
        self._net_table = QTableWidget(0, 7)
        self._net_table.setHorizontalHeaderLabels(
            ["Reach","From node","Type","To node","Type","Length (km)","Reach type"])
        self._net_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._net_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._net_table.setAlternatingRowColors(True)
        self._net_table.setSortingEnabled(True)
        layout.addWidget(self._net_table)
        return w

    def _node_type_label(self, node):
        from .core.attributes import Basin, Confluence
        if isinstance(node, Basin):      return "Sub-area"
        if isinstance(node, Confluence): return "Outlet" if node.isOut else "Confluence"
        return "-"

    def _node_type_color(self, label):
        return {'Outlet':'#f97316','Confluence':'#3b82f6','Sub-area':'#22c55e'}.get(label,'#888')

    def _build_network_table(self):
        self._net_table.setSortingEnabled(False)
        self._net_table.setRowCount(0)
        self._net_warn.setText("")
        if not self._reach_row.layer() or not self._junc_row.layer():
            self._net_warn.setText("Select Reach and Confluences layers first.")
            return
        try:
            from .core.qgis_builder import build_reaches, build_confluences, build_basins
            from .core.catchment import Catchment
            from .core.attributes import Basin, Confluence
            reaches     = build_reaches(self._reach_row.layer(),
                                        self._reach_row.field("id"),
                                        self._reach_row.field("slope"),
                                        self._reach_row.field("type"))
            confluences = build_confluences(self._junc_row.layer(),
                                            self._junc_row.field("id"),
                                            self._junc_row.field("out"))
            lc = self._cent_row.layer(); lb = self._basin_row.layer()
            basins = build_basins(lc, lb, self._cent_row.field("id"),
                                  self._cent_row.field("fi")) if lc and lb else []
            catchment = Catchment(confluences, basins, reaches); catchment.connect()
            sentinel = catchment._endSentinel
            nv = len(catchment._vertices)
            rows_data = []; warnings = []; total_len = 0.0
            for j, reach in enumerate(catchment._edges):
                up = dn = None
                for i in range(nv):
                    ds = catchment._incidenceMatrixDS[i][j]
                    if ds != sentinel:
                        up = catchment._vertices[i]
                        dn = catchment._vertices[ds]
                        break
                lkm = reach.length() / 1000.0; total_len += lkm
                ul = self._node_type_label(up) if up else "-"
                dl = self._node_type_label(dn) if dn else "-"
                if up is None or dn is None:
                    warnings.append(f"'{reach.name}' not connected")
                rows_data.append({'reach': reach.name,
                    'from': up.name if up else "?", 'from_type': ul,
                    'to':   dn.name if dn else "?", 'to_type':   dl,
                    'len_km': lkm,
                    'rtype': reach.type.name.capitalize() if reach.type else "Natural"})
            self._net_table.setRowCount(len(rows_data))
            for i, r in enumerate(rows_data):
                self._net_table.setItem(i, 0, QTableWidgetItem(r['reach']))
                self._net_table.setItem(i, 1, QTableWidgetItem(r['from']))
                ft = QTableWidgetItem(r['from_type'])
                ft.setForeground(QColor(self._node_type_color(r['from_type'])))
                self._net_table.setItem(i, 2, ft)
                self._net_table.setItem(i, 3, QTableWidgetItem(r['to']))
                tt = QTableWidgetItem(r['to_type'])
                tt.setForeground(QColor(self._node_type_color(r['to_type'])))
                self._net_table.setItem(i, 4, tt)
                li = QTableWidgetItem(f"{r['len_km']:.3f}")
                li.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._net_table.setItem(i, 5, li)
                self._net_table.setItem(i, 6, QTableWidgetItem(r['rtype']))
            n_j = sum(1 for v in catchment._vertices if isinstance(v, Confluence))
            n_b = sum(1 for v in catchment._vertices if isinstance(v, Basin))
            self._net_reaches_lbl.setText(f"Reaches: {len(reaches)}")
            self._net_junctions_lbl.setText(f"Confluences: {n_j}")
            self._net_basins_lbl.setText(f"Sub-areas: {n_b}")
            self._net_length_lbl.setText(f"Total: {total_len:.2f} km")
            if warnings:
                self._net_warn.setText("Warning: " + "  ".join(warnings[:5]))
            self._net_table.setSortingEnabled(True)
        except Exception as e:
            self._net_warn.setText(f"Error: {e}")

    def _export_network_csv(self):
        if self._net_table.rowCount() == 0:
            QMessageBox.warning(self, "Network", "Build the network first."); return
        path, _ = QFileDialog.getSaveFileName(self, "Export", "", "CSV (*.csv)")
        if not path: return
        import csv as csv_mod
        with open(path, 'w', newline='') as f:
            w = csv_mod.writer(f)
            w.writerow(["Reach","From node","From type","To node","To type",
                        "Length (km)","Reach type"])
            for row in range(self._net_table.rowCount()):
                w.writerow([self._net_table.item(row, c).text()
                             for c in range(self._net_table.columnCount())])
        QMessageBox.information(self, "Network", f"Exported:\n{path}")

    # ---- Inputs -----------------------------------------------------------
    def _tab_inputs(self):
        w = QWidget(); layout = QVBoxLayout(w)

        def _section(title, browse_fn):
            box = QGroupBox(title); bl = QVBoxLayout(box)
            row = QHBoxLayout()
            edit = QLineEdit(); edit.setPlaceholderText("(not selected)")
            edit.setReadOnly(True)
            btn  = QPushButton("Browse..."); btn.setFixedWidth(80)
            btn.clicked.connect(lambda: browse_fn(edit))
            row.addWidget(edit); row.addWidget(btn); bl.addLayout(row)
            info = QLabel(""); info.setWordWrap(True)
            info.setStyleSheet("color:gray;font-size:8pt;")
            bl.addWidget(info); layout.addWidget(box)
            return edit, info

        self._arr_edit, self._arr_info = _section(
            "1.  ARR Data Hub TXT  (IL, CL, LONGARF)", self._browse_arr)
        self._ifd_edit, self._ifd_info = _section(
            "2.  IFD CSV  (BOM All Design Rainfall Depth)", self._browse_ifd)
        self._tp_edit,  self._tp_info  = _section(
            "3.  Temporal Pattern CSV  (ARR 2016 increments)", self._browse_tp)

        layout.addStretch()
        return w

    def _browse_arr(self, edit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ARR TXT", "", "Text files (*.txt);;All (*)")
        if not path: return
        edit.setText(path)
        try:
            from .core.storm import parse_arr_txt
            data = parse_arr_txt(path)
            self._storm_data['arr'] = data
            il = data.get('il'); cl = data.get('cl')
            larf = data.get('longarf', {})
            zone = larf.get('zone', '')
            prm  = '  '.join(f"{k}={v}" for k, v in larf.items() if k != 'zone')
            self._arr_info.setText(
                f"IL = {il} mm   CL = {cl} mm/hr\n"
                f"LONGARF zone: {zone}   {prm}")
            if il is not None: self._il.setValue(il)
            if cl is not None: self._cl.setValue(cl)
        except Exception as e:
            self._arr_info.setText(f"Error: {e}")

    def _browse_ifd(self, edit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open IFD CSV", "", "CSV files (*.csv);;All (*)")
        if not path: return
        edit.setText(path)
        try:
            from .core.storm import parse_ifd_csv
            aeps, rows, meta = parse_ifd_csv(path)
            self._storm_data['ifd_aeps'] = aeps
            self._storm_data['ifd_rows'] = rows
            loc = meta.get('location', '')
            lat = meta.get('lat'); lon = meta.get('lon')
            self._ifd_info.setText(
                f"{loc}  lat={lat:.4f}  lon={lon:.4f}  "
                f"{len(rows)} durations x {len(aeps)} AEPs"
                if lat else f"{len(rows)} durations x {len(aeps)} AEPs")
            self._refresh_run_tab()
        except Exception as e:
            self._ifd_info.setText(f"Error: {e}")

    def _browse_tp(self, edit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Temporal Pattern CSV", "", "CSV files (*.csv);;All (*)")
        if not path: return
        edit.setText(path)
        try:
            from .core.storm import parse_temporal_patterns
            patterns = parse_temporal_patterns(path)
            self._storm_data['tp_patterns'] = patterns
            durs  = sorted(set(p['duration_min'] for p in patterns))
            bands = sorted(set(p['aep_band'] for p in patterns))
            self._tp_info.setText(
                f"{len(patterns)} patterns  |  {len(durs)} durations  |  "
                f"bands: {', '.join(bands)}")
            self._refresh_run_tab()
        except Exception as e:
            self._tp_info.setText(f"Error: {e}")

    # ---- Parameters -------------------------------------------------------
    def _tab_parameters(self):
        w = QWidget(); layout = QVBoxLayout(w)

        rbox = QGroupBox("RORB Routing Parameters")
        rf = QFormLayout(rbox)
        self._kc = self._spin(0.01, 100.0, 1.0, 3, "hr/km")
        self._m  = self._spin(0.1,   2.0,  0.8, 2, "(dimensionless)")
        rf.addRow("kc (routing coefficient):", self._kc)
        rf.addRow("m  (non-linearity exponent):", self._m)
        layout.addWidget(rbox)

        lbox = QGroupBox("Loss Model  (auto-filled from ARR TXT)")
        lf = QFormLayout(lbox)
        self._il = self._spin(0, 500, 25.0, 1, "mm")
        self._cl = self._spin(0,  50,  2.5, 2, "mm/hr")
        lf.addRow("Initial loss (IL):", self._il)
        lf.addRow("Continuing loss (CL):", self._cl)
        layout.addWidget(lbox)

        arf_box = QGroupBox("Areal Reduction Factor")
        af = QFormLayout(arf_box)
        self._arf = self._spin(0.01, 1.0, 1.0, 3, "")
        self._arf.setToolTip(
            "Applied to all events.  Check RORB or ARR for site-specific values.")
        af.addRow("ARF (manual):", self._arf)
        layout.addWidget(arf_box)
        layout.addStretch()
        return w

    # ---- Run --------------------------------------------------------------
    def _tab_run(self):
        w = QWidget(); root = QVBoxLayout(w)

        # AEP group
        aep_outer = QGroupBox("AEP Selection  (populated after IFD CSV is loaded)")
        aep_vbox  = QVBoxLayout(aep_outer)
        aep_btns  = QHBoxLayout()
        aep_all   = QPushButton("All");  aep_all.clicked.connect(
            lambda: self._check_all(self._aep_chks, True))
        aep_none  = QPushButton("None"); aep_none.clicked.connect(
            lambda: self._check_all(self._aep_chks, False))
        aep_btns.addStretch(); aep_btns.addWidget(aep_all); aep_btns.addWidget(aep_none)
        aep_vbox.addLayout(aep_btns)
        self._aep_inner = QWidget()
        self._aep_grid  = QGridLayout(self._aep_inner)
        aep_vbox.addWidget(self._aep_inner)
        root.addWidget(aep_outer)

        # Duration group (scrollable)
        dur_outer = QGroupBox("Duration Selection  (common durations in IFD + Temporal CSV)")
        dur_vbox  = QVBoxLayout(dur_outer)
        dur_btns  = QHBoxLayout()
        dur_all   = QPushButton("All");  dur_all.clicked.connect(
            lambda: self._check_all(self._dur_chks, True))
        dur_none  = QPushButton("None"); dur_none.clicked.connect(
            lambda: self._check_all(self._dur_chks, False))
        dur_btns.addStretch(); dur_btns.addWidget(dur_all); dur_btns.addWidget(dur_none)
        dur_vbox.addLayout(dur_btns)
        self._dur_scroll = QScrollArea()
        self._dur_scroll.setWidgetResizable(True)
        self._dur_scroll.setMaximumHeight(120)
        self._dur_inner = QWidget()
        self._dur_grid  = QGridLayout(self._dur_inner)
        self._dur_scroll.setWidget(self._dur_inner)
        dur_vbox.addWidget(self._dur_scroll)
        root.addWidget(dur_outer)

        # TP + Run
        ctrl_box = QGroupBox("Temporal Patterns")
        ctrl_f   = QFormLayout(ctrl_box)
        tp_row   = QHBoxLayout()
        self._tp_from = QSpinBox(); self._tp_from.setRange(1, 10); self._tp_from.setValue(1)
        self._tp_to   = QSpinBox(); self._tp_to.setRange(1, 10);   self._tp_to.setValue(10)
        tp_row.addWidget(QLabel("TP")); tp_row.addWidget(self._tp_from)
        tp_row.addWidget(QLabel("to")); tp_row.addWidget(self._tp_to)
        tp_row.addStretch()
        ctrl_f.addRow("TP range:", tp_row)
        root.addWidget(ctrl_box)

        self._run_progress = QProgressBar()
        self._run_progress.setRange(0, 100)
        self._run_status   = QLabel("Ready.")
        self._run_status.setStyleSheet("color:gray;font-size:9pt;")
        self._run_btn = QPushButton("Run All Combinations")
        self._run_btn.setMinimumHeight(38)
        self._run_btn.clicked.connect(self._on_run_batch)

        root.addWidget(self._run_progress)
        root.addWidget(self._run_status)
        root.addWidget(self._run_btn)

        hint = QLabel(
            "Runs every AEP x Duration x TP combination using the catchment layers "
            "and parameters above.  Click a row in Results to view its hydrograph.")
        hint.setWordWrap(True); hint.setStyleSheet("color:gray;font-size:8pt;")
        root.addWidget(hint)
        return w

    def _check_all(self, chk_dict, state):
        for cb in chk_dict.values():
            cb.setChecked(state)

    def _refresh_run_tab(self):
        aeps     = self._storm_data.get('ifd_aeps', [])
        ifd_rows = self._storm_data.get('ifd_rows', [])
        patterns = self._storm_data.get('tp_patterns', [])

        # Rebuild AEP checkboxes
        while self._aep_grid.count():
            item = self._aep_grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._aep_chks.clear()
        tp_bands = set(p['aep_band'] for p in patterns) if patterns else set()
        cols = 6
        for idx, aep in enumerate(aeps):
            try:
                from .core.storm import aep_to_band
                band = aep_to_band(aep)
            except Exception:
                band = 'intermediate'
            cb = QCheckBox(aep)
            cb.setChecked(band in tp_bands or not tp_bands)
            self._aep_chks[aep] = cb
            self._aep_grid.addWidget(cb, idx // cols, idx % cols)

        # Rebuild Duration checkboxes
        ifd_mins = {r['minutes'] for r in ifd_rows}
        tp_mins  = {p['duration_min'] for p in patterns}
        common   = sorted(ifd_mins & tp_mins) if (tp_mins and ifd_mins) else sorted(ifd_mins)
        while self._dur_grid.count():
            item = self._dur_grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._dur_chks.clear()
        dur_labels = {r['minutes']: r['label'] for r in ifd_rows}
        for idx, mins in enumerate(common):
            label = dur_labels.get(mins, f"{mins} min")
            cb = QCheckBox(label); cb.setChecked(True)
            self._dur_chks[mins] = cb
            self._dur_grid.addWidget(cb, idx // cols, idx % cols)

    # ---- Batch run --------------------------------------------------------
    def _on_run_batch(self):
        errors = self._validate_layers()
        if errors:
            QMessageBox.warning(self, "RORB", "\n".join(errors)); return
        if not self._storm_data.get('ifd_rows'):
            QMessageBox.warning(self, "RORB", "Load IFD CSV in the Inputs tab."); return
        if not self._storm_data.get('tp_patterns'):
            QMessageBox.warning(self, "RORB", "Load Temporal Pattern CSV in the Inputs tab."); return

        sel_aeps = [a for a, cb in self._aep_chks.items() if cb.isChecked()]
        sel_durs = [(self._dur_chks[m].text(), m)
                    for m in self._dur_chks if self._dur_chks[m].isChecked()]
        tp_nums  = list(range(int(self._tp_from.value()),
                               int(self._tp_to.value()) + 1))
        if not sel_aeps:
            QMessageBox.warning(self, "RORB", "Select at least one AEP."); return
        if not sel_durs:
            QMessageBox.warning(self, "RORB", "Select at least one duration."); return

        combos = [(aep, lbl, mins, tp)
                  for aep in sel_aeps
                  for lbl, mins in sel_durs
                  for tp in tp_nums]

        self._batch_rows    = []
        self._critical_rows = []
        self._batch_table.setRowCount(0)
        self._crit_table.setRowCount(0)
        self._log.clear()
        self._log_msg(
            f"Running {len(combos)} combinations "
            f"({len(sel_aeps)} AEPs x {len(sel_durs)} durations x {len(tp_nums)} TPs)...")
        self._run_progress.setRange(0, len(combos))
        self._run_progress.setValue(0)
        self._run_status.setText("Starting...")
        self._run_btn.setEnabled(False)
        self._tabs.setCurrentIndex(6)   # Results

        cp = dict(
            reach_lyr = self._reach_row.layer(),
            cent_lyr  = self._cent_row.layer(),
            junc_lyr  = self._junc_row.layer(),
            basin_lyr = self._basin_row.layer(),
            fld_rid   = self._reach_row.field("id"),
            fld_slope = self._reach_row.field("slope"),
            fld_type  = self._reach_row.field("type"),
            fld_cid   = self._cent_row.field("id"),
            fld_fi    = self._cent_row.field("fi"),
            fld_jid   = self._junc_row.field("id"),
            fld_out   = self._junc_row.field("out"),
        )
        mp = dict(kc=self._kc.value(), m=self._m.value(),
                  il=self._il.value(), cl=self._cl.value())

        self._worker = BatchWorker(
            cp, mp, combos,
            self._storm_data['ifd_rows'],
            self._storm_data['tp_patterns'],
            self._arf.value())
        self._worker.progress.connect(self._on_batch_progress)
        self._worker.partial_result.connect(self._on_batch_partial)
        self._worker.log_msg.connect(self._log_msg)
        self._worker.finished.connect(self._on_batch_finished)
        self._worker.error.connect(self._on_batch_error)
        self._worker.start()

    def _on_batch_progress(self, current, total, label):
        self._run_progress.setValue(current)
        self._run_status.setText(f"{current}/{total}   {label}")

    def _on_batch_partial(self, r):
        self._batch_rows.append(r)
        row = self._batch_table.rowCount()
        self._batch_table.insertRow(row)
        self._batch_table.setItem(row, 0, QTableWidgetItem(r['aep']))
        self._batch_table.setItem(row, 1, QTableWidgetItem(r['dur_label']))
        tp_i = QTableWidgetItem(str(r['tp_num']))
        tp_i.setTextAlignment(Qt.AlignCenter)
        self._batch_table.setItem(row, 2, tp_i)
        for col, key, dec in ((3,'depth_mm',1),(4,'peak',3),(5,'ttp',2)):
            it = QTableWidgetItem(f"{r[key]:.{dec}f}")
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._batch_table.setItem(row, col, it)

    def _on_batch_finished(self):
        self._run_btn.setEnabled(True)
        self._run_status.setText(f"Done. {len(self._batch_rows)} events.")
        self._log_msg(f"Completed. {len(self._batch_rows)} events.")
        self._compute_critical_events()

    def _compute_critical_events(self):
        """
        ARR 2016 Book 4 Ch3 s2.3:
        - For each AEP x Duration: compute MEAN peak across all TPs
        - Critical duration = duration with highest mean peak (per AEP)
        - Representative TP = TP whose peak is closest to that mean
        """
        from collections import defaultdict
        if not self._batch_rows:
            return

        # Group by AEP -> duration -> list of rows
        groups = defaultdict(lambda: defaultdict(list))
        for r in self._batch_rows:
            groups[r['aep']][(r['dur_label'], r['dur_min'])].append(r)

        critical = []
        for aep, durations in groups.items():
            # Mean peak per duration
            dur_summary = {}
            for (dur_label, dur_min), rows in durations.items():
                peaks = [r['peak'] for r in rows]
                dur_summary[(dur_label, dur_min)] = (float(np.mean(peaks)), rows)

            # Critical duration = highest mean peak
            (crit_lbl, crit_min), (mean_peak, crit_rows) = max(
                dur_summary.items(), key=lambda x: x[1][0])

            # Representative TP = closest to mean
            rep_row = min(crit_rows, key=lambda r: abs(r['peak'] - mean_peak))

            # All mean peaks for this AEP (for logging)
            dur_means = {lbl: round(v, 3) for (lbl, _), (v, _) in dur_summary.items()}

            critical.append({
                'aep':       aep,
                'crit_dur':  crit_lbl,
                'crit_min':  crit_min,
                'mean_peak': mean_peak,
                'rep_tp':    rep_row['tp_num'],
                'rep_peak':  rep_row['peak'],
                'ttp':       rep_row['ttp'],
                'depth_mm':  rep_row['depth_mm'],
                'hydro':     rep_row['hydro'],
                'time':      rep_row['time'],
                'n_tp':      len(crit_rows),
                'dur_means': dur_means,
            })

        # Sort by AEP (frequent -> rare)
        def _aep_sort_key(r):
            a = r['aep'].replace('%', '')
            try:
                return -float(a)   # higher % = more frequent = sort first
            except ValueError:
                return 0.0

        critical.sort(key=_aep_sort_key)
        self._critical_rows = critical

        # Populate critical table
        self._crit_table.setRowCount(0)
        for r in critical:
            row = self._crit_table.rowCount()
            self._crit_table.insertRow(row)
            self._crit_table.setItem(row, 0, QTableWidgetItem(r['aep']))
            self._crit_table.setItem(row, 1, QTableWidgetItem(r['crit_dur']))
            tp_item = QTableWidgetItem(str(r['rep_tp']))
            tp_item.setTextAlignment(Qt.AlignCenter)
            self._crit_table.setItem(row, 2, tp_item)
            n_item = QTableWidgetItem(str(r['n_tp']))
            n_item.setTextAlignment(Qt.AlignCenter)
            self._crit_table.setItem(row, 3, n_item)
            for col, key, dec in ((4,'mean_peak',3),(5,'rep_peak',3),(6,'ttp',2)):
                it = QTableWidgetItem(f"{r[key]:.{dec}f}")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._crit_table.setItem(row, col, it)

        # Log summary
        self._log_msg("\n--- Critical Events (ARR 2016 mean-TP method) ---")
        for r in critical:
            self._log_msg(
                f"  {r['aep']:<8}  crit dur = {r['crit_dur']:<10}  "
                f"mean peak = {r['mean_peak']:.3f} m3/s  "
                f"rep TP = {r['rep_tp']}  (rep peak = {r['rep_peak']:.3f} m3/s)")

    def _on_critical_row_selected(self):
        rows = self._crit_table.selectionModel().selectedRows()
        if not rows or not HAS_MPL:
            return
        idx = rows[0].row()
        if idx >= len(self._critical_rows):
            return
        r = self._critical_rows[idx]
        if not r['hydro']:
            return
        self._ax.clear()
        t = r['time'][:len(r['hydro'])]
        self._ax.plot(t, r['hydro'], color='steelblue', linewidth=2,
                      label=f"Rep TP{r['rep_tp']} ({r['rep_peak']:.3f} m3/s)")
        # also draw mean peak as horizontal dashed line
        self._ax.axhline(r['mean_peak'], color='gray', linewidth=1,
                         linestyle='--', label=f"Mean of {r['n_tp']} TPs ({r['mean_peak']:.3f} m3/s)")
        self._ax.set_xlabel('Time (hr)')
        self._ax.set_ylabel('Flow (m3/s)')
        self._ax.set_title(
            f"{r['aep']}  Critical duration: {r['crit_dur']}  "
            f"Rep TP: {r['rep_tp']}  depth: {r['depth_mm']:.1f} mm", fontsize=9)
        self._ax.grid(True, alpha=0.3)
        self._ax.legend(fontsize=8)
        pk_t = r['ttp'] if r['ttp'] > 0 else (t[-1] if t else 10)
        self._ax.set_xlim(0, min(t[-1] if t else 100, pk_t * 3 + 5))
        self._canvas.draw()

    def _on_batch_error(self, msg):
        self._run_btn.setEnabled(True)
        self._run_status.setText("Error - see log")
        self._log_msg("ERROR:\n" + msg)

    def _on_batch_row_selected(self):
        rows = self._batch_table.selectionModel().selectedRows()
        if not rows or not HAS_MPL: return
        idx = rows[0].row()
        if idx >= len(self._batch_rows): return
        r = self._batch_rows[idx]
        if not r['hydro']: return
        self._ax.clear()
        t = r['time'][:len(r['hydro'])]
        self._ax.plot(t, r['hydro'], color='steelblue', linewidth=2)
        self._ax.set_xlabel('Time (hr)')
        self._ax.set_ylabel('Flow (m3/s)')
        self._ax.set_title(
            f"{r['aep']}  {r['dur_label']}  TP{r['tp_num']}  "
            f"depth={r['depth_mm']:.1f} mm  "
            f"peak={r['peak']:.3f} m3/s  TTP={r['ttp']:.2f} hr", fontsize=9)
        self._ax.grid(True, alpha=0.3)
        pk_t = r['ttp'] if r['ttp'] > 0 else (t[-1] if t else 10)
        self._ax.set_xlim(0, min(t[-1] if t else 100, pk_t * 3 + 5))
        self._canvas.draw()

    # ---- Files tab --------------------------------------------------------
    def _tab_files(self):
        w = QWidget(); layout = QVBoxLayout(w)
        box = QGroupBox("RORB Output Files")
        form = QFormLayout(box)

        def _row(label, filt):
            row = QHBoxLayout()
            edit = QLineEdit(); edit.setPlaceholderText("(not selected)")
            btn  = QPushButton("Browse..."); btn.setFixedWidth(80)
            row.addWidget(edit); row.addWidget(btn)
            btn.clicked.connect(lambda _=False, e=edit, f=filt:
                e.setText(QFileDialog.getOpenFileName(w, label, '', f)[0]))
            form.addRow(label, row)
            return edit

        self._f_catg = _row("Catchment (.catg) *",   "RORB catchment (*.catg);;All (*)")
        self._f_out  = _row("Output file (.out) *",  "RORB output (*.out);;All (*)")
        self._f_stm  = _row("Storm file (.stm)",     "RORB storm (*.stm);;All (*)")
        self._f_csv  = _row("RORB CSV (comparison)", "CSV (*.csv);;All (*)")
        layout.addWidget(box)

        note = QLabel("* Required.  .stm overrides rainfall with higher-precision data.")
        note.setStyleSheet("color:gray;font-size:8pt;")
        layout.addWidget(note)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Plot:"))
        self._node_combo = QComboBox(); self._node_combo.setMinimumWidth(240)
        self._node_combo.currentIndexChanged.connect(self._on_node_changed)
        ctrl.addWidget(self._node_combo)
        self._show_rorb_chk = QCheckBox("Show RORB comparison")
        self._show_rorb_chk.setChecked(True)
        self._show_rorb_chk.stateChanged.connect(self._on_node_changed)
        ctrl.addWidget(self._show_rorb_chk)
        ctrl.addStretch()
        layout.addLayout(ctrl)
        layout.addStretch()
        return w

    def _on_run_files(self):
        catg = self._f_catg.text().strip()
        out  = self._f_out.text().strip()
        if not catg or not os.path.exists(catg):
            self._log.append("ERROR: .catg not found."); return
        if not out or not os.path.exists(out):
            self._log.append("ERROR: .out not found."); return
        self._log.clear()
        self._run_files_btn.setEnabled(False)
        self._run_files_btn.setText("Running...")
        self._tabs.setCurrentIndex(6)
        self._worker = FilesWorker(
            catg, out,
            self._f_stm.text().strip() or None,
            self._f_csv.text().strip() or None)
        self._worker.log_msg.connect(self._log_msg)
        self._worker.finished.connect(self._on_files_finished)
        self._worker.error.connect(self._on_files_error)
        self._worker.start()

    def _on_files_finished(self, results):
        self._results = results
        self._run_files_btn.setEnabled(True)
        self._run_files_btn.setText("Run from Files")
        self._node_combo.blockSignals(True)
        self._node_combo.clear()
        self._node_combo.addItem("-- All print nodes --", ('node', None))
        for name in results.get('hydros', {}):
            self._node_combo.addItem(name, ('node', name))
        self._node_combo.insertSeparator(self._node_combo.count())
        for reach in results.get('reach_results', []):
            self._node_combo.addItem(reach['label'], ('reach', reach['reach_num']))
        self._node_combo.blockSignals(False)
        if HAS_MPL:
            self._plot_files(results)

    def _on_files_error(self, msg):
        self._run_files_btn.setEnabled(True)
        self._run_files_btn.setText("Run from Files")
        self._log_msg("ERROR:\n" + msg)

    def _on_node_changed(self, _=None):
        if self._results and HAS_MPL:
            self._plot_files(self._results)

    def _plot_files(self, results):
        self._ax.clear()
        sel      = self._node_combo.currentData()
        sel_type, sel_val = (sel or ('node', None))
        hydros   = results.get('hydros', {})
        reach_res= results.get('reach_results', [])
        time     = results.get('time', [])
        rorb     = results.get('rorb_nodes', {})
        rorb_t   = results.get('rorb_time', time)
        show_r   = self._show_rorb_chk.isChecked()
        COLORS   = ['steelblue','darkorange','forestgreen','crimson',
                    'mediumpurple','sienna','teal','deeppink']
        if sel_type == 'reach' and sel_val is not None:
            reach = next((r for r in reach_res if r['reach_num'] == sel_val), None)
            if reach:
                q = reach['hydro']
                self._ax.plot(time[:len(q)], q, color='steelblue', linewidth=2)
                self._ax.set_title(
                    f"{reach['label']}  k={results['kc']}x{reach['kr']:.3f}={reach['k']:.3f}  "
                    f"peak={float(np.max(q)):.3f} m3/s", fontsize=8)
        else:
            nodes = [sel_val] if sel_val else list(hydros.keys())
            for ci, name in enumerate(nodes):
                q = hydros.get(name)
                if q is None: continue
                color = COLORS[ci % len(COLORS)]
                self._ax.plot(time[:len(q)], q, color=color, linewidth=2,
                              label=f"{name} (Engine)")
                if show_r and name in rorb:
                    rq = rorb[name]; rt = rorb_t[:len(rq)]
                    self._ax.plot(rt, rq, color=color, linewidth=1.2,
                                  linestyle='--', label=f"{name} (RORB)")
            if hydros:
                all_q  = np.concatenate(list(hydros.values()))
                pk_t   = time[int(np.argmax(all_q))] if len(all_q) else 0
                self._ax.set_xlim(0, min(time[-1] if time else 100, pk_t*3+5))
        self._ax.set_xlabel('Time (hr)')
        self._ax.set_ylabel('Flow (m3/s)')
        self._ax.grid(True, alpha=0.3)
        self._ax.legend(fontsize=8)
        self._canvas.draw()

    # ---- Results ----------------------------------------------------------
    def _tab_results(self):
        w = QWidget(); layout = QVBoxLayout(w)

        self._log = QTextEdit()
        self._log.setReadOnly(True); self._log.setMaximumHeight(100)
        self._log.setFont(QFont("Courier New", 8))
        layout.addWidget(QLabel("Log:")); layout.addWidget(self._log)

        tbl_hdr = QHBoxLayout()
        tbl_hdr.addWidget(QLabel("Results  (click a row to plot its hydrograph):"))
        tbl_hdr.addStretch()
        exp_btn = QPushButton("Export CSV...")
        exp_btn.clicked.connect(self._export_batch_csv)
        tbl_hdr.addWidget(exp_btn)
        layout.addLayout(tbl_hdr)

        self._batch_table = QTableWidget(0, 6)
        self._batch_table.setHorizontalHeaderLabels(
            ["AEP", "Duration", "TP",
             "Catchment depth (mm)", "Outlet peak (m3/s)", "Time to peak (hr)"])
        self._batch_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._batch_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._batch_table.setAlternatingRowColors(True)
        self._batch_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._batch_table.setMaximumHeight(200)
        self._batch_table.selectionModel().selectionChanged.connect(
            self._on_batch_row_selected)
        layout.addWidget(self._batch_table)

        # ── Critical events (ARR 2016 mean-TP method) ─────────────────────
        crit_hdr = QHBoxLayout()
        crit_hdr.addWidget(QLabel(
            "Critical Events  (ARR 2016: mean of TPs -> critical duration -> rep TP):"))
        crit_hdr.addStretch()
        crit_exp = QPushButton("Export Critical CSV...")
        crit_exp.clicked.connect(self._export_critical_csv)
        crit_hdr.addWidget(crit_exp)
        layout.addLayout(crit_hdr)

        self._crit_table = QTableWidget(0, 7)
        self._crit_table.setHorizontalHeaderLabels([
            "AEP", "Critical Duration", "Rep TP", "# TPs",
            "Mean Peak (m3/s)", "Rep Peak (m3/s)", "Time to Peak (hr)"])
        self._crit_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._crit_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._crit_table.setAlternatingRowColors(True)
        self._crit_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._crit_table.setMaximumHeight(160)
        self._crit_table.selectionModel().selectionChanged.connect(
            self._on_critical_row_selected)
        layout.addWidget(self._crit_table)

        if HAS_MPL:
            self._fig    = Figure(figsize=(6, 3), tight_layout=True)
            self._ax     = self._fig.add_subplot(111)
            self._canvas = FigureCanvas(self._fig)
            layout.addWidget(self._canvas)
        else:
            self._ax = None; self._canvas = None
            layout.addWidget(QLabel("(install matplotlib to see charts)"))
        return w

    def _export_batch_csv(self):
        if not self._batch_rows:
            QMessageBox.warning(self, "Results", "No results yet."); return
        path, _ = QFileDialog.getSaveFileName(self, "Export", "", "CSV (*.csv)")
        if not path: return
        import csv as csv_mod
        with open(path, 'w', newline='') as f:
            w = csv_mod.writer(f)
            w.writerow(["AEP","Duration","TP","Catchment depth (mm)",
                        "Outlet peak (m3/s)","Time to peak (hr)"])
            for r in self._batch_rows:
                w.writerow([r['aep'], r['dur_label'], r['tp_num'],
                            f"{r['depth_mm']:.1f}",
                            f"{r['peak']:.4f}", f"{r['ttp']:.3f}"])
        QMessageBox.information(self, "Results", f"Exported:\n{path}")

    def _export_critical_csv(self):
        if not self._critical_rows:
            QMessageBox.warning(self, "Results", "Run batch first."); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export critical events", "", "CSV (*.csv)")
        if not path: return
        import csv as csv_mod
        with open(path, 'w', newline='') as f:
            w = csv_mod.writer(f)
            w.writerow(["AEP", "Critical Duration", "Rep TP", "Num TPs",
                        "Mean Peak (m3/s)", "Rep Peak (m3/s)",
                        "Catchment depth (mm)", "Time to peak (hr)"])
            for r in self._critical_rows:
                w.writerow([r['aep'], r['crit_dur'], r['rep_tp'], r['n_tp'],
                            f"{r['mean_peak']:.4f}", f"{r['rep_peak']:.4f}",
                            f"{r['depth_mm']:.1f}", f"{r['ttp']:.3f}"])
        QMessageBox.information(self, "Results", f"Exported:\n{path}")

    # ---- .catg export -----------------------------------------------------
    def _on_export_catg(self):
        errors = self._validate_layers()
        if errors:
            QMessageBox.warning(self, "RORB Builder", "\n".join(errors)); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export .catg", "", "RORB Control File (*.catg)")
        if not path: return
        try:
            import pyromb
            from .vector_layer import QVectorLayer, SnappedQVectorLayer, snap_reach_endpoints
            rf = list(self._reach_row.layer().getFeatures())
            cf = list(self._junc_row.layer().getFeatures())
            cf2= list(self._cent_row.layer().getFeatures())
            rm = {'id':self._reach_row.field("id"),'t':self._reach_row.field("type"),
                  's':self._reach_row.field("slope")}
            cm = {'id':self._cent_row.field("id"),'fi':self._cent_row.field("fi")}
            jm = {'id':self._junc_row.field("id"),'out':self._junc_row.field("out")}
            snapped = snap_reach_endpoints(rf,[cf,cf2],tolerance=self._snap_tol.value())
            rv = SnappedQVectorLayer(rf, snapped, rm)
            cv = QVectorLayer(cf2, cm)
            jv = QVectorLayer(cf,  jm)
            bv = QVectorLayer(self._basin_row.layer())
            b  = pyromb.Builder()
            tr = b.reach(rv); tc = b.confluence(jv); tb = b.basin(cv, bv)
            catchment = pyromb.Catchment(tc, tb, tr); catchment.connect()
            trav = pyromb.Traveller(catchment)
            with open(path, 'w') as f:
                f.write(trav.getVector(pyromb.RORB()))
            QMessageBox.information(self, "RORB Builder", f"Exported:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    # ---- Helpers ----------------------------------------------------------
    def _log_msg(self, msg):
        self._log.append(msg)

    def _validate_layers(self):
        errors = []
        if not self._reach_row.layer():  errors.append("No reach layer.")
        if not self._cent_row.layer():   errors.append("No centroid layer.")
        if not self._junc_row.layer():   errors.append("No confluence layer.")
        if not self._basin_row.layer():  errors.append("No sub-catchment layer.")
        if not self._reach_row.field("id"):  errors.append("Reach id field required.")
        if not self._cent_row.field("id"):   errors.append("Centroid id field required.")
        if not self._junc_row.field("id"):   errors.append("Confluence id field required.")
        if not self._junc_row.field("out"):  errors.append("Confluence outlet field required.")
        return errors

    @staticmethod
    def _spin(lo, hi, val, dec, suffix=""):
        s = QDoubleSpinBox()
        s.setRange(lo, hi); s.setValue(val); s.setDecimals(dec)
        if suffix: s.setSuffix("  " + suffix)
        s.setMinimumWidth(180)
        return s
