import os
import traceback
import numpy as np

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QTextEdit, QTabWidget,
    QWidget, QDoubleSpinBox, QComboBox, QTableWidget, QTableWidgetItem,
    QSplitter, QRadioButton, QButtonGroup, QHeaderView, QFileDialog,
    QMessageBox, QLineEdit, QCheckBox,
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


class SimWorker(QThread):
    log_msg = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, params):
        super().__init__()
        self.params = params

    def run(self):
        try:
            p = self.params
            from .core.qgis_builder import build_reaches, build_confluences, build_basins
            from .core.catchment import Catchment
            from .core.rainfall import (uniform_pattern, triangular_pattern, parse_pattern)
            from .core.simulation import run

            self.log_msg.emit("Building reaches…")
            reaches = build_reaches(p['reach_lyr'], p['fld_rid'],
                                    p['fld_slope'], p['fld_type'])
            self.log_msg.emit(f"  {len(reaches)} reach(es)")

            self.log_msg.emit("Building junctions…")
            confluences = build_confluences(p['junc_lyr'], p['fld_jid'], p['fld_out'])
            outlets = [c for c in confluences if c.isOut]
            self.log_msg.emit(f"  {len(confluences)} junction(s), {len(outlets)} outlet(s)")
            if len(outlets) != 1:
                self.error.emit(f"Need exactly 1 outlet junction (found {len(outlets)}).")
                return

            self.log_msg.emit("Building basins…")
            basins = build_basins(p['cent_lyr'], p['basin_lyr'],
                                  p['fld_cid'], p['fld_fi'])
            self.log_msg.emit(f"  {len(basins)} basin(s), total area "
                              f"{sum(b.area for b in basins):.2f} km²")

            self.log_msg.emit("Connecting topology…")
            catchment = Catchment(confluences, basins, reaches)
            catchment.connect()

            n_steps_storm = max(1, round(p['duration'] / p['dt']))
            if p['rain_mode'] == 'uniform':
                rain = uniform_pattern(p['total_mm'], n_steps_storm)
            elif p['rain_mode'] == 'triangular':
                rain = triangular_pattern(p['total_mm'], n_steps_storm, p['peak_pos'])
            else:
                rain = parse_pattern(p['custom_pattern'], p['total_mm'], n_steps_storm)

            self.log_msg.emit(
                f"Rainfall: {p['total_mm']:.1f} mm over {p['duration']:.1f} hr "
                f"({n_steps_storm} steps × {p['dt']:.2f} hr)"
            )
            self.log_msg.emit("Running simulation…")

            results = run(
                catchment=catchment,
                kc=p['kc'], m=p['m'], dt_hr=p['dt'],
                rainfall_mm=rain,
                loss_model=p['loss_model'],
                il_mm=p['il'], cl_mm_hr=p['cl'],
                prop_loss=p['prop_loss'],
                n_steps=n_steps_storm * 4,
            )

            outlet_peak = next(
                (r['peak_flow'] for r in results.values() if r['is_outlet']), 0.0)
            self.log_msg.emit(f"\nOutlet peak flow: {outlet_peak:.2f} m³/s")
            self.finished.emit(results)

        except Exception:
            self.error.emit(traceback.format_exc())


class FilesWorker(QThread):
    """Run the validated engine from .catg / .out / .stm files."""
    log_msg  = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, catg, out, stm, csv_path):
        super().__init__()
        self.catg     = catg
        self.out      = out
        self.stm      = stm or None
        self.csv_path = csv_path or None

    def run(self):
        try:
            from .core.engine import run_from_files, parse_rorb_csv

            self.log_msg.emit(f"Parsing .catg:  {self.catg}")
            self.log_msg.emit(f"Parsing .out:   {self.out}")
            if self.stm:
                self.log_msg.emit(f"Parsing .stm:   {self.stm}")

            res = run_from_files(self.catg, self.out, self.stm)

            self.log_msg.emit(
                f"\nkc={res['kc']}  m={res['m']}  IL={res['il']} mm  "
                f"CL={res['cl']} mm/hr  dt={res['dt']} hr"
            )
            self.log_msg.emit(f"Steps: {res['n_steps']}  "
                              f"Nodes: {list(res['hydros'].keys())}")

            # RORB CSV comparison (optional)
            rorb_nodes, rorb_time, _ = {}, [], None
            if self.csv_path and os.path.exists(self.csv_path):
                self.log_msg.emit(f"Loading RORB CSV: {self.csv_path}")
                rorb_nodes, rorb_time, _ = parse_rorb_csv(self.csv_path)

            res['rorb_nodes'] = rorb_nodes
            res['rorb_time']  = rorb_time
            res['source']     = 'files'

            for name, q in res['hydros'].items():
                rorb_peak = res['rorb_peaks'].get(name)
                eng_peak  = float(np.max(q))
                diff = (eng_peak - rorb_peak) / rorb_peak * 100 if rorb_peak else None
                diff_str = f"  (RORB {rorb_peak:.3f}, Δ{diff:+.1f}%)" if diff is not None else ""
                self.log_msg.emit(f"  {name}: peak = {eng_peak:.3f} m³/s{diff_str}")

            self.finished.emit(res)

        except Exception:
            self.error.emit(traceback.format_exc())


class RorbModelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RORB Builder")
        self.setMinimumSize(960, 720)
        self._results = None
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_catchment(), "Catchment")
        self._tabs.addTab(self._tab_parameters(), "Parameters")
        self._tabs.addTab(self._tab_rainfall(), "Rainfall")
        self._tabs.addTab(self._tab_storm(), "Storm Setup")
        self._tabs.addTab(self._tab_files(), "Files")
        self._tabs.addTab(self._tab_results(), "Results")
        root.addWidget(self._tabs)

        btn_row = QHBoxLayout()
        self._run_btn = QPushButton("▶  Run Model")
        self._run_btn.setMinimumHeight(38)
        self._run_btn.setDefault(True)
        self._run_btn.clicked.connect(self._on_run)
        self._run_files_btn = QPushButton("▶  Run from Files")
        self._run_files_btn.setMinimumHeight(38)
        self._run_files_btn.clicked.connect(self._on_run_files)
        catg_btn = QPushButton("Export .catg…")
        catg_btn.setMinimumHeight(38)
        catg_btn.clicked.connect(self._on_export_catg)
        csv_btn = QPushButton("Export CSV…")
        csv_btn.clicked.connect(self._export_csv)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addStretch()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._run_files_btn)
        btn_row.addWidget(catg_btn)
        btn_row.addWidget(csv_btn)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _tab_catchment(self):
        w = QWidget()
        layout = QVBoxLayout(w)
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

        note = QLabel("Reach type: 1=Natural  2=Unlined  3=Lined  4=Drowned  "
                      "(blank → Natural, slope only needed for types 2 & 3)")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 8pt;")
        layout.addWidget(note)

        snap_row = QHBoxLayout()
        snap_row.addWidget(QLabel("Snap tolerance (map units):"))
        self._snap_tol = QDoubleSpinBox()
        self._snap_tol.setRange(0.0, 1000.0)
        self._snap_tol.setValue(1.0)
        self._snap_tol.setDecimals(2)
        self._snap_tol.setSuffix("  m")
        self._snap_tol.setToolTip("Reach endpoints within this distance of a node are snapped to it. Set to 0 to disable.")
        snap_row.addWidget(self._snap_tol)
        snap_row.addStretch()
        layout.addLayout(snap_row)
        layout.addStretch()
        return w

    def _tab_parameters(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        rbox = QGroupBox("RORB Routing Parameters")
        rf = QFormLayout(rbox)
        self._kc = self._spin(0.01, 100.0, 1.0, 3, "hr/km")
        self._m  = self._spin(0.1,   2.0,  0.8, 2, "(dimensionless)")
        rf.addRow("kc (routing coefficient):", self._kc)
        rf.addRow("m  (non-linearity exponent):", self._m)
        tip = QLabel("Typical values: kc = 0.5–5 hr/km for rural Australian catchments. "
                     "m = 0.8 (RORB default).")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray; font-size: 8pt;")
        rf.addRow("", tip)
        layout.addWidget(rbox)

        lbox = QGroupBox("Loss Model")
        lf = QFormLayout(lbox)
        self._loss_combo = QComboBox()
        self._loss_combo.addItems(["IL/CL (Initial/Continuing Loss)", "Proportional Loss"])
        self._loss_combo.currentIndexChanged.connect(self._on_loss_changed)
        lf.addRow("Model:", self._loss_combo)
        self._il   = self._spin(0, 500, 25.0, 1, "mm")
        self._cl   = self._spin(0,  50,  2.5, 2, "mm/hr")
        self._prop = self._spin(0,   1,  0.3, 2, "(fraction, 0=no loss, 1=all lost)")
        self._prop.setEnabled(False)
        lf.addRow("Initial loss (IL):", self._il)
        lf.addRow("Continuing loss (CL):", self._cl)
        lf.addRow("Proportional loss fraction:", self._prop)
        layout.addWidget(lbox)

        dtbox = QGroupBox("Time Discretisation")
        dtf = QFormLayout(dtbox)
        self._dt = self._spin(0.01, 24.0, 0.5, 2, "hours")
        dtf.addRow("Time step (dt):", self._dt)
        layout.addWidget(dtbox)
        layout.addStretch()
        return w

    def _tab_rainfall(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        dbox = QGroupBox("Design Storm")
        df = QFormLayout(dbox)
        self._total_mm = self._spin(1, 10000, 100.0, 1, "mm")
        self._duration = self._spin(0.1, 720,   6.0, 1, "hours")
        df.addRow("Total rainfall depth:", self._total_mm)
        df.addRow("Storm duration:", self._duration)
        layout.addWidget(dbox)

        pbox = QGroupBox("Temporal Pattern")
        pf = QFormLayout(pbox)
        self._pattern_group = QButtonGroup(self)
        rb_uniform    = QRadioButton("Uniform (constant intensity)")
        rb_triangular = QRadioButton("Triangular (peak at 1/3)")
        rb_custom     = QRadioButton("Custom (enter values below)")
        rb_uniform.setChecked(True)
        self._pattern_group.addButton(rb_uniform,    0)
        self._pattern_group.addButton(rb_triangular, 1)
        self._pattern_group.addButton(rb_custom,     2)
        self._pattern_group.buttonClicked.connect(self._on_pattern_changed)

        self._peak_pos = self._spin(0.01, 0.99, 0.33, 2, "(fraction of duration)")
        self._peak_pos.setEnabled(False)
        self._custom_pattern = QTextEdit()
        self._custom_pattern.setPlaceholderText(
            "Enter rainfall depths [mm] per time step, comma or space separated.\n"
            "Example (10 steps): 2, 5, 12, 18, 15, 10, 6, 4, 3, 2\n"
            "Will be rescaled to the total depth entered above."
        )
        self._custom_pattern.setMaximumHeight(100)
        self._custom_pattern.setEnabled(False)

        pf.addRow("", rb_uniform)
        pf.addRow("", rb_triangular)
        pf.addRow("Peak position:", self._peak_pos)
        pf.addRow("", rb_custom)
        pf.addRow("Custom pattern:", self._custom_pattern)
        layout.addWidget(pbox)
        layout.addStretch()
        return w

    # ── Storm Setup tab ───────────────────────────────────────────────────────

    def _tab_storm(self):
        w = QWidget()
        root = QVBoxLayout(w)
        self._storm_data = {}   # holds parsed ifd/tp/arr data

        def browse_row(parent_layout, label, filt, on_load):
            box = QGroupBox(label)
            bl = QVBoxLayout(box)
            row = QHBoxLayout()
            edit = QLineEdit()
            edit.setPlaceholderText("(not selected)")
            edit.setReadOnly(True)
            btn = QPushButton("Browse…")
            btn.setFixedWidth(80)
            btn.clicked.connect(lambda: self._storm_browse(edit, filt, on_load))
            row.addWidget(edit)
            row.addWidget(btn)
            bl.addLayout(row)
            info = QLabel("")
            info.setStyleSheet("color: gray; font-size: 8pt;")
            info.setWordWrap(True)
            bl.addWidget(info)
            parent_layout.addWidget(box)
            return edit, info

        # ── IFD CSV ──────────────────────────────────────────────────────────
        self._ifd_edit, self._ifd_info = browse_row(
            root, "IFD CSV  (BOM 'All Design Rainfall Depth')",
            "CSV files (*.csv);;All (*)", self._storm_load_ifd)

        # ── Temporal pattern CSV ─────────────────────────────────────────────
        self._tp_edit, self._tp_info = browse_row(
            root, "Temporal Pattern CSV  (ARR 2016 increments)",
            "CSV files (*.csv);;All (*)", self._storm_load_tp)

        # ── ARR Data Hub TXT ─────────────────────────────────────────────────
        self._arr_edit, self._arr_info = browse_row(
            root, "ARR Data Hub TXT",
            "Text files (*.txt);;All (*)", self._storm_load_arr)

        # ── Storm selection ──────────────────────────────────────────────────
        sel_box = QGroupBox("Storm Selection")
        sel_form = QFormLayout(sel_box)

        self._storm_aep_cb   = QComboBox()
        self._storm_dur_cb   = QComboBox()
        self._storm_tp_spin  = QDoubleSpinBox()
        self._storm_tp_spin.setRange(1, 10); self._storm_tp_spin.setDecimals(0)
        self._storm_tp_spin.setValue(1); self._storm_tp_spin.setSuffix("  (1 – 10)")
        self._storm_arf_spin = QDoubleSpinBox()
        self._storm_arf_spin.setRange(0.01, 1.0); self._storm_arf_spin.setDecimals(3)
        self._storm_arf_spin.setValue(1.0)
        self._storm_arf_spin.setToolTip(
            "Areal Reduction Factor — from RORB / ARR. "
            "ARF parameters shown in ARR box above for reference.")

        sel_form.addRow("AEP:",             self._storm_aep_cb)
        sel_form.addRow("Duration:",        self._storm_dur_cb)
        sel_form.addRow("Temporal Pattern #:", self._storm_tp_spin)
        sel_form.addRow("ARF (manual):",    self._storm_arf_spin)

        self._storm_preview = QLabel("—")
        self._storm_preview.setWordWrap(True)
        self._storm_preview.setStyleSheet("font-size: 9pt; color: #2c6fad;")
        sel_form.addRow("Storm preview:", self._storm_preview)

        root.addWidget(sel_box)

        for cb in (self._storm_aep_cb, self._storm_dur_cb):
            cb.currentIndexChanged.connect(self._storm_update_preview)
        for sp in (self._storm_tp_spin, self._storm_arf_spin):
            sp.valueChanged.connect(self._storm_update_preview)

        # ── Generate button ──────────────────────────────────────────────────
        gen_btn = QPushButton("▶  Generate Storm & Run Model")
        gen_btn.setMinimumHeight(36)
        gen_btn.clicked.connect(self._storm_generate_and_run)
        root.addWidget(gen_btn)

        note = QLabel(
            "Workflow: IFD depth × ARF × temporal pattern % → rainfall series → "
            "IL/CL from ARR → run engine on current catchment layers."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 8pt;")
        root.addWidget(note)
        root.addStretch()
        return w

    def _storm_browse(self, edit, filt, callback):
        path, _ = QFileDialog.getOpenFileName(self, "Open file", "", filt)
        if path:
            edit.setText(path)
            callback(path)

    def _storm_load_ifd(self, path):
        try:
            from .core.storm import parse_ifd_csv
            aeps, rows, meta = parse_ifd_csv(path)
            self._storm_data['ifd_aeps'] = aeps
            self._storm_data['ifd_rows'] = rows

            self._storm_aep_cb.blockSignals(True)
            self._storm_aep_cb.clear()
            self._storm_aep_cb.addItems(aeps)
            self._storm_aep_cb.blockSignals(False)

            self._storm_dur_cb.blockSignals(True)
            self._storm_dur_cb.clear()
            for r in rows:
                self._storm_dur_cb.addItem(r['label'], r['minutes'])
            self._storm_dur_cb.blockSignals(False)

            loc = meta.get('location') or ''
            lat = meta.get('lat')
            lon = meta.get('lon')
            self._ifd_info.setText(
                f"{loc}  lat={lat:.4f}  lon={lon:.4f}  "
                f"({len(rows)} durations, {len(aeps)} AEPs)"
                if lat else f"{len(rows)} durations, {len(aeps)} AEPs"
            )
            self._storm_update_preview()
        except Exception as e:
            self._ifd_info.setText(f"Error: {e}")

    def _storm_load_tp(self, path):
        try:
            from .core.storm import parse_temporal_patterns
            patterns = parse_temporal_patterns(path)
            self._storm_data['tp_patterns'] = patterns
            durs  = sorted(set(p['duration_min'] for p in patterns))
            bands = sorted(set(p['aep_band'] for p in patterns))
            self._tp_info.setText(
                f"{len(patterns)} patterns  |  "
                f"durations: {durs[:5]}{'…' if len(durs) > 5 else ''}  |  "
                f"bands: {', '.join(bands)}"
            )
            self._storm_update_preview()
        except Exception as e:
            self._tp_info.setText(f"Error: {e}")

    def _storm_load_arr(self, path):
        try:
            from .core.storm import parse_arr_txt
            data = parse_arr_txt(path)
            self._storm_data['arr'] = data
            il = data.get('il')
            cl = data.get('cl')
            larf = data.get('longarf', {})
            zone = larf.get('zone', '')
            params_str = '  '.join(f'{k}={v}' for k, v in larf.items()
                                   if k != 'zone') if larf else '(none)'
            self._arr_info.setText(
                f"IL={il} mm   CL={cl} mm/hr\n"
                f"LONGARF zone: {zone}   params: {params_str}"
            )
            # Auto-fill IL/CL in Parameters tab
            if il is not None:
                self._il.setValue(il)
            if cl is not None:
                self._cl.setValue(cl)
        except Exception as e:
            self._arr_info.setText(f"Error: {e}")

    def _storm_update_preview(self):
        try:
            from .core.storm import get_ifd_depth, get_temporal_pattern, aep_to_band
            rows     = self._storm_data.get('ifd_rows', [])
            patterns = self._storm_data.get('tp_patterns', [])
            if not rows:
                self._storm_preview.setText("Load IFD CSV to see preview.")
                return

            aep      = self._storm_aep_cb.currentText()
            dur_min  = self._storm_dur_cb.currentData()
            tp_num   = int(self._storm_tp_spin.value())
            arf      = self._storm_arf_spin.value()
            band     = aep_to_band(aep)

            depth = get_ifd_depth(rows, aep, dur_min)
            if depth is None:
                self._storm_preview.setText("Burst depth not found for this AEP/Duration.")
                return

            catchment_depth = depth * arf
            lines = [
                f"AEP band: {band}",
                f"Burst depth: {depth:.1f} mm",
                f"ARF: {arf:.3f}  →  Catchment depth: {catchment_depth:.1f} mm",
            ]

            if patterns:
                incs, dt_min = get_temporal_pattern(patterns, dur_min, band, tp_num)
                if incs:
                    dt_hr   = dt_min / 60.0
                    n_steps = len(incs)
                    lines += [
                        f"Temporal pattern: {n_steps} steps × {dt_min} min (dt={dt_hr:.4f} hr)",
                        f"Pattern total: {sum(incs):.1f}%  "
                        f"(max step: {max(incs):.1f}% = {catchment_depth*max(incs)/100:.1f} mm)",
                    ]
                else:
                    lines.append(
                        f"No pattern for {dur_min} min / {band}  "
                        f"(check TP CSV covers this duration)"
                    )

            self._storm_preview.setText("\n".join(lines))
        except Exception as e:
            self._storm_preview.setText(f"Preview error: {e}")

    def _storm_generate_and_run(self):
        from .core.storm import (get_ifd_depth, get_temporal_pattern,
                                  aep_to_band, build_rainfall_series)
        rows     = self._storm_data.get('ifd_rows', [])
        patterns = self._storm_data.get('tp_patterns', [])

        if not rows:
            QMessageBox.warning(self, "Storm Setup", "Load an IFD CSV first.")
            return
        if not patterns:
            QMessageBox.warning(self, "Storm Setup", "Load a Temporal Pattern CSV first.")
            return

        aep     = self._storm_aep_cb.currentText()
        dur_min = self._storm_dur_cb.currentData()
        tp_num  = int(self._storm_tp_spin.value())
        arf     = self._storm_arf_spin.value()
        band    = aep_to_band(aep)

        depth = get_ifd_depth(rows, aep, dur_min)
        if depth is None:
            QMessageBox.warning(self, "Storm Setup",
                                f"No IFD depth found for AEP={aep}, Duration={dur_min} min.")
            return

        incs, dt_min = get_temporal_pattern(patterns, dur_min, band, tp_num)
        if not incs:
            QMessageBox.warning(self, "Storm Setup",
                                f"No temporal pattern for {dur_min} min / {band} / TP{tp_num}.")
            return

        rain_ts = build_rainfall_series(depth, arf, incs)
        dt_hr   = dt_min / 60.0

        # Validate layers
        errors = self._validate_layers()
        if errors:
            self._tabs.setCurrentIndex(5)   # Results tab
            self._log.clear()
            for e in errors:
                self._log_msg(f"✗ {e}")
            return

        # Build params (same as _on_run but with storm-generated rainfall)
        params = dict(
            reach_lyr=self._reach_row.layer(),
            cent_lyr=self._cent_row.layer(),
            junc_lyr=self._junc_row.layer(),
            basin_lyr=self._basin_row.layer(),
            fld_rid=self._reach_row.field("id"),
            fld_slope=self._reach_row.field("slope"),
            fld_type=self._reach_row.field("type"),
            fld_cid=self._cent_row.field("id"),
            fld_fi=self._cent_row.field("fi"),
            fld_jid=self._junc_row.field("id"),
            fld_out=self._junc_row.field("out"),
            kc=self._kc.value(),
            m=self._m.value(),
            dt=dt_hr,
            loss_model='il_cl' if self._loss_combo.currentIndex() == 0 else 'proportional',
            il=self._il.value(),
            cl=self._cl.value(),
            prop_loss=self._prop.value(),
            total_mm=sum(rain_ts),
            duration=len(rain_ts) * dt_hr,
            rain_mode='custom',
            peak_pos=0.33,
            custom_pattern=','.join(f'{v:.4f}' for v in rain_ts),
            _storm_label=f"{aep}  {dur_min} min  TP{tp_num}  ARF={arf:.3f}",
        )

        self._log.clear()
        self._log_msg(
            f"Storm: {aep}  {self._storm_dur_cb.currentText()}  TP{tp_num}\n"
            f"  burst depth={depth:.1f} mm  ARF={arf:.3f}  "
            f"catchment depth={depth*arf:.1f} mm\n"
            f"  dt={dt_hr:.4f} hr  steps={len(rain_ts)}"
        )
        self._run_btn.setEnabled(False)
        self._run_btn.setText("Running…")
        self._tabs.setCurrentIndex(5)   # Results tab

        self._worker = SimWorker(params)
        self._worker.log_msg.connect(self._log_msg)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _tab_files(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        box = QGroupBox("RORB Output Files")
        form = QFormLayout(box)

        def _file_row(label, filt):
            row = QHBoxLayout()
            edit = QLineEdit()
            edit.setPlaceholderText("(not selected)")
            btn = QPushButton("Browse…")
            btn.setFixedWidth(80)
            row.addWidget(edit)
            row.addWidget(btn)
            btn.clicked.connect(lambda _=False, e=edit, f=filt:
                e.setText(QFileDialog.getOpenFileName(w, label, '', f)[0]))
            form.addRow(label, row)
            return edit

        self._f_catg = _file_row("Catchment (.catg) *",    "RORB catchment (*.catg);;All (*)")
        self._f_out  = _file_row("Output file (.out) *",   "RORB output (*.out);;All (*)")
        self._f_stm  = _file_row("Storm file (.stm)",      "RORB storm (*.stm);;All (*)")
        self._f_csv  = _file_row("RORB CSV (comparison)",  "CSV (*.csv);;All (*)")

        layout.addWidget(box)

        note = QLabel(
            "* Required.  .stm provides higher-precision rainfall than .out.\n"
            "RORB CSV overlays the RORB app hydrographs on the chart for comparison."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 8pt;")
        layout.addWidget(note)
        layout.addStretch()
        return w

    def _tab_results(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(110)
        self._log.setFont(QFont("Courier New", 8))
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self._log)

        # Node selector row
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Plot node:"))
        self._node_combo = QComboBox()
        self._node_combo.setMinimumWidth(200)
        self._node_combo.currentIndexChanged.connect(self._on_node_changed)
        ctrl_row.addWidget(self._node_combo)
        self._show_rorb_chk = QCheckBox("Show RORB comparison")
        self._show_rorb_chk.setChecked(True)
        self._show_rorb_chk.stateChanged.connect(self._on_node_changed)
        ctrl_row.addWidget(self._show_rorb_chk)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        splitter = QSplitter(Qt.Horizontal)
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Node", "Type", "Peak (m³/s)", "RORB (m³/s)", "Time to Peak (hr)"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setMinimumWidth(360)
        splitter.addWidget(self._table)

        if HAS_MPL:
            self._fig = Figure(figsize=(5, 3), tight_layout=True)
            self._ax  = self._fig.add_subplot(111)
            self._canvas = FigureCanvas(self._fig)
            splitter.addWidget(self._canvas)
        else:
            splitter.addWidget(QLabel("(matplotlib not available — install to see chart)"))

        splitter.setSizes([380, 560])
        layout.addWidget(splitter)
        return w

    @staticmethod
    def _spin(lo, hi, val, dec, suffix=""):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setValue(val)
        s.setDecimals(dec)
        if suffix:
            s.setSuffix("  " + suffix)
        s.setMinimumWidth(180)
        return s

    def _on_loss_changed(self, idx):
        is_ilcl = (idx == 0)
        self._il.setEnabled(is_ilcl)
        self._cl.setEnabled(is_ilcl)
        self._prop.setEnabled(not is_ilcl)

    def _on_pattern_changed(self, btn):
        mode = self._pattern_group.id(btn)
        self._peak_pos.setEnabled(mode == 1)
        self._custom_pattern.setEnabled(mode == 2)

    def _log_msg(self, msg):
        self._log.append(msg)

    def _validate_layers(self):
        errors = []
        if not self._reach_row.layer():  errors.append("No reach layer selected.")
        if not self._cent_row.layer():   errors.append("No centroid layer selected.")
        if not self._junc_row.layer():   errors.append("No confluence layer selected.")
        if not self._basin_row.layer():  errors.append("No sub-catchment layer selected.")
        if not self._reach_row.field("id"):  errors.append("Reach: id field required.")
        if not self._cent_row.field("id"):   errors.append("Centroid: id field required.")
        if not self._junc_row.field("id"):   errors.append("Confluence: id field required.")
        if not self._junc_row.field("out"):  errors.append("Confluence: outlet field required.")
        return errors

    def _on_run_files(self):
        catg = self._f_catg.text().strip()
        out  = self._f_out.text().strip()
        stm  = self._f_stm.text().strip() or None
        csv_path = self._f_csv.text().strip() or None

        if not catg or not os.path.exists(catg):
            self._log.append("✗ .catg file not found."); return
        if not out or not os.path.exists(out):
            self._log.append("✗ .out file not found."); return

        self._log.clear()
        self._table.setRowCount(0)
        if HAS_MPL:
            self._ax.clear(); self._canvas.draw()

        self._run_files_btn.setEnabled(False)
        self._run_files_btn.setText("Running…")
        self._tabs.setCurrentIndex(5)   # Results tab

        self._worker = FilesWorker(catg, out, stm, csv_path)
        self._worker.log_msg.connect(self._log_msg)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_run(self):
        self._log.clear()
        self._table.setRowCount(0)
        if HAS_MPL:
            self._ax.clear()
            self._canvas.draw()

        errors = self._validate_layers()
        if errors:
            for e in errors:
                self._log_msg(f"✗ {e}")
            return

        mode_id = self._pattern_group.checkedId()
        pattern_mode = {0: 'uniform', 1: 'triangular', 2: 'custom'}[mode_id]
        if pattern_mode == 'custom' and not self._custom_pattern.toPlainText().strip():
            self._log_msg("✗ Custom pattern: please enter rainfall depths.")
            return

        params = dict(
            reach_lyr=self._reach_row.layer(),
            cent_lyr=self._cent_row.layer(),
            junc_lyr=self._junc_row.layer(),
            basin_lyr=self._basin_row.layer(),
            fld_rid=self._reach_row.field("id"),
            fld_slope=self._reach_row.field("slope"),
            fld_type=self._reach_row.field("type"),
            fld_cid=self._cent_row.field("id"),
            fld_fi=self._cent_row.field("fi"),
            fld_jid=self._junc_row.field("id"),
            fld_out=self._junc_row.field("out"),
            kc=self._kc.value(),
            m=self._m.value(),
            dt=self._dt.value(),
            loss_model='il_cl' if self._loss_combo.currentIndex() == 0 else 'proportional',
            il=self._il.value(),
            cl=self._cl.value(),
            prop_loss=self._prop.value(),
            total_mm=self._total_mm.value(),
            duration=self._duration.value(),
            rain_mode=pattern_mode,
            peak_pos=self._peak_pos.value(),
            custom_pattern=self._custom_pattern.toPlainText(),
        )

        self._run_btn.setEnabled(False)
        self._run_btn.setText("Running…")
        self._tabs.setCurrentIndex(5)   # Results tab

        self._worker = SimWorker(params)
        self._worker.log_msg.connect(self._log_msg)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_finished(self, results):
        self._results = results
        self._run_btn.setEnabled(True)
        self._run_btn.setText("▶  Run Model")
        self._run_files_btn.setEnabled(True)
        self._run_files_btn.setText("▶  Run from Files")

        source = results.get('source', 'layers')
        if source == 'files':
            self._populate_table_files(results)
        else:
            self._populate_table(results)

        # Populate node combo
        self._node_combo.blockSignals(True)
        self._node_combo.clear()
        if source == 'files':
            self._node_combo.addItem("— All print nodes —", ('node', None))
            for name in results.get('hydros', {}):
                self._node_combo.addItem(name, ('node', name))
            # Add separator then individual reaches
            self._node_combo.insertSeparator(self._node_combo.count())
            for reach in results.get('reach_results', []):
                self._node_combo.addItem(reach['label'], ('reach', reach['reach_num']))
        else:
            self._node_combo.addItem("— Outlet only —", ('node', None))
            for r in results.values():
                self._node_combo.addItem(r['name'], ('node', r['name']))
        self._node_combo.blockSignals(False)

        if HAS_MPL:
            self._plot(results)

    def _on_error(self, msg):
        self._run_btn.setEnabled(True)
        self._run_btn.setText("▶  Run Model")
        self._run_files_btn.setEnabled(True)
        self._run_files_btn.setText("▶  Run from Files")
        self._log_msg("ERROR:")
        self._log_msg(msg)

    def _on_node_changed(self, _=None):
        if self._results and HAS_MPL:
            self._plot(self._results)

    # ── Table: shapefile-based results ────────────────────────────────────────

    def _populate_table(self, results):
        rows = sorted(results.values(),
                      key=lambda r: (0 if r['node_type'] == 'Junction' else 1,
                                     -r['peak_flow']))
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            name_item = QTableWidgetItem(r['name'])
            if r['is_outlet']:
                name_item.setForeground(QColor('#0066cc'))
                font = name_item.font(); font.setBold(True)
                name_item.setFont(font)
            self._table.setItem(i, 0, name_item)
            self._table.setItem(i, 1, QTableWidgetItem(r['node_type']))
            self._table.setItem(i, 2, QTableWidgetItem(f"{r['peak_flow']:.3f}"))
            self._table.setItem(i, 3, QTableWidgetItem(""))
            self._table.setItem(i, 4, QTableWidgetItem(f"{r['time_to_peak']:.2f}"))

    # ── Table: file-based results ─────────────────────────────────────────────

    def _populate_table_files(self, results):
        hydros     = results['hydros']
        time_axis  = results['time']
        dt         = results['dt']
        rorb_peaks = results.get('rorb_peaks', {})

        self._table.setRowCount(len(hydros))
        for i, (name, q) in enumerate(hydros.items()):
            eng_peak  = float(np.max(q))
            ttp_idx   = int(np.argmax(q))
            ttp       = time_axis[ttp_idx] if ttp_idx < len(time_axis) else ttp_idx * dt
            rorb_peak = rorb_peaks.get(name)

            self._table.setItem(i, 0, QTableWidgetItem(name))
            self._table.setItem(i, 1, QTableWidgetItem("Junction"))
            self._table.setItem(i, 2, QTableWidgetItem(f"{eng_peak:.3f}"))
            self._table.setItem(i, 3, QTableWidgetItem(
                f"{rorb_peak:.3f}" if rorb_peak is not None else ""))
            self._table.setItem(i, 4, QTableWidgetItem(f"{ttp:.2f}"))

            if rorb_peak is not None:
                diff = abs(eng_peak - rorb_peak) / rorb_peak * 100
                color = QColor('#2d6a2d') if diff <= 2 else (
                    QColor('#6a4e2d') if diff <= 5 else QColor('#6a2d2d'))
                for col in range(5):
                    item = self._table.item(i, col)
                    if item:
                        item.setBackground(color)

    # ── Chart ─────────────────────────────────────────────────────────────────

    def _plot(self, results):
        self._ax.clear()
        source    = results.get('source', 'layers')
        selected  = self._node_combo.currentData()
        show_rorb = self._show_rorb_chk.isChecked()

        COLORS = ['steelblue', 'darkorange', 'forestgreen', 'crimson',
                  'mediumpurple', 'sienna', 'deeppink', 'teal']

        if source == 'files':
            hydros       = results['hydros']
            time_axis    = results['time']
            reach_res    = results.get('reach_results', [])
            rorb_nodes   = results.get('rorb_nodes', {})
            rorb_time    = results.get('rorb_time', time_axis)
            sel_type, sel_val = (selected or ('node', None))

            if sel_type == 'reach' and sel_val is not None:
                # Single reach selected
                reach = next((r for r in reach_res if r['reach_num'] == sel_val), None)
                if reach:
                    q = reach['hydro']
                    self._ax.plot(time_axis[:len(q)], q,
                                  color='steelblue', linewidth=2, label=reach['label'])
                    peak = float(np.max(q))
                    ttp  = time_axis[int(np.argmax(q))]
                    self._ax.set_title(
                        f"{reach['label']}\n"
                        f"k = kc × kr = {results['kc']} × {reach['kr']:.3f} = {reach['k']:.3f}   "
                        f"peak = {peak:.3f} m³/s  at {ttp:.2f} hr",
                        fontsize=8)
                    self._ax.set_xlim(0, min(time_axis[-1], ttp * 3 + 5))
            else:
                # Print nodes (all or one)
                nodes_to_plot = ([sel_val] if sel_val else list(hydros.keys()))
                for ci, name in enumerate(nodes_to_plot):
                    q = hydros.get(name)
                    if q is None:
                        continue
                    color = COLORS[ci % len(COLORS)]
                    self._ax.plot(time_axis[:len(q)], q, color=color, linewidth=2,
                                  label=f"{name} (Engine)")
                    if show_rorb and name in rorb_nodes:
                        rq = rorb_nodes[name]
                        rt = rorb_time[:len(rq)] if rorb_time else time_axis[:len(rq)]
                        self._ax.plot(rt, rq, color=color, linewidth=1.2,
                                      linestyle='--', label=f"{name} (RORB)")

                all_q = np.concatenate([q for q in hydros.values()])
                peak_t = time_axis[int(np.argmax(all_q))] if len(all_q) else 0
                self._ax.set_xlim(0, min(time_axis[-1], peak_t * 3 + 5))

        else:
            # Shapefile-based: plot selected node or outlet
            _, node_name = (selected or ('node', None))
            if node_name:
                r = next((v for v in results.values() if v['name'] == node_name), None)
            else:
                r = next((v for v in results.values() if v['is_outlet']), None)
            if r:
                self._ax.plot(r['time'], r['hydro'],
                              color='steelblue', linewidth=2, label=r['name'])
                self._ax.set_title(f"{r['name']}  Peak = {r['peak_flow']:.2f} m³/s")

        self._ax.set_xlabel('Time (hr)')
        self._ax.set_ylabel('Flow (m³/s)')
        self._ax.grid(True, alpha=0.3)
        self._ax.legend(fontsize=8)
        self._canvas.draw()

    def _on_export_catg(self):
        errors = self._validate_layers()
        if errors:
            QMessageBox.warning(self, "RORB Builder", "\n".join(errors))
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export RORB control file", "", "RORB Control File (*.catg)")
        if not path:
            return

        try:
            import pyromb
            from .vector_layer import QVectorLayer, SnappedQVectorLayer, snap_reach_endpoints

            reach_features = list(self._reach_row.layer().getFeatures())
            conf_features  = list(self._junc_row.layer().getFeatures())
            cent_features  = list(self._cent_row.layer().getFeatures())

            reach_map = {
                'id': self._reach_row.field("id"),
                't':  self._reach_row.field("type"),
                's':  self._reach_row.field("slope"),
            }
            cent_map = {
                'id': self._cent_row.field("id"),
                'fi': self._cent_row.field("fi"),
            }
            conf_map = {
                'id':  self._junc_row.field("id"),
                'out': self._junc_row.field("out"),
            }

            snapped_geoms = snap_reach_endpoints(
                reach_features, [conf_features, cent_features],
                tolerance=self._snap_tol.value())
            reach_vector = SnappedQVectorLayer(reach_features, snapped_geoms, reach_map)
            cent_vector  = QVectorLayer(cent_features,  cent_map)
            conf_vector  = QVectorLayer(conf_features,  conf_map)
            basin_vector = QVectorLayer(self._basin_row.layer())

            builder   = pyromb.Builder()
            tr = builder.reach(reach_vector)
            tc = builder.confluence(conf_vector)
            tb = builder.basin(cent_vector, basin_vector)

            catchment = pyromb.Catchment(tc, tb, tr)
            catchment.connect()
            traveller = pyromb.Traveller(catchment)

            with open(path, 'w') as f:
                f.write(traveller.getVector(pyromb.RORB()))

            QMessageBox.information(self, "RORB Builder", f"Exported successfully:\n{path}")

        except Exception as e:
            QMessageBox.critical(self, "RORB Builder — Export Failed", str(e))

    def _export_csv(self):
        if not self._results:
            self._log_msg("No results to export — run the model first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export results", "", "CSV files (*.csv)")
        if not path:
            return
        import csv as csv_mod

        source = self._results.get('source', 'layers')

        with open(path, 'w', newline='') as f:
            w = csv_mod.writer(f)

            if source == 'files':
                hydros    = self._results['hydros']
                time_axis = self._results['time']
                peaks     = self._results.get('rorb_peaks', {})
                w.writerow(['Node', 'Engine_Peak_m3s', 'RORB_Peak_m3s', 'TimeToPeak_hr'])
                for name, q in hydros.items():
                    pk = float(np.max(q))
                    tti = int(np.argmax(q))
                    ttp = time_axis[tti] if tti < len(time_axis) else tti * self._results['dt']
                    w.writerow([name, f"{pk:.4f}",
                                 f"{peaks[name]:.4f}" if name in peaks else '',
                                 f"{ttp:.3f}"])
                w.writerow([])
                w.writerow(['Time_hr'] + list(hydros.keys()))
                for i, t in enumerate(time_axis):
                    w.writerow([f"{t:.3f}"] + [f"{q[i]:.4f}" for q in hydros.values()])
            else:
                first = next(iter(self._results.values()))
                time = first['time']
                w.writerow(['Node', 'Type', 'Area_km2', 'Peak_m3s', 'TimeToPeak_hr'])
                for r in self._results.values():
                    w.writerow([r['name'], r['node_type'],
                                 f"{r['area_km2']:.4f}" if r['area_km2'] else '',
                                 f"{r['peak_flow']:.4f}", f"{r['time_to_peak']:.3f}"])
                w.writerow([])
                junctions = [r for r in self._results.values()
                             if r['node_type'] == 'Junction']
                w.writerow(['Time_hr'] + [r['name'] for r in junctions])
                for i, t in enumerate(time):
                    w.writerow([f"{t:.3f}"] + [f"{r['hydro'][i]:.4f}" for r in junctions])

        self._log_msg(f"Exported to {path}")
