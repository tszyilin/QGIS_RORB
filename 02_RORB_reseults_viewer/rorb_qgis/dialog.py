import os

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QTextEdit, QTabWidget, QWidget,
    QDoubleSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox,
    QLineEdit, QCheckBox,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox

from .compat import (
    AlignRightVCenter, AlignCenter,
    AllNonFixedFieldsGrow, HeaderStretch, NoEditTriggers,
    LayerFilterLine, LayerFilterPoint, LayerFilterPolygon,
    DialogAccepted,
)


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
        lbl.setAlignment(AlignRightVCenter)
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
class RorbModelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("RORB Builder")
        self.setMinimumSize(900, 600)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_catchment(), "Catchment")
        self._tabs.addTab(self._tab_network(),   "Network")
        root.addWidget(self._tabs)

        # Status log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(60)
        self._log.setPlaceholderText("Status messages...")
        root.addWidget(self._log)

        btn_row = QHBoxLayout()
        catg_btn = QPushButton("Export .catg...")
        catg_btn.setMinimumHeight(36)
        catg_btn.clicked.connect(self._on_export_catg)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addStretch()
        btn_row.addWidget(catg_btn)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ---- Catchment --------------------------------------------------------
    def _tab_catchment(self):
        w = QWidget(); layout = QVBoxLayout(w)
        box = QGroupBox("Input Layers & Field Mapping")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(AllNonFixedFieldsGrow)

        self._reach_row = _LayerRow(LayerFilterLine)
        self._reach_row.add_field("id:", "id")
        self._reach_row.add_field("type(1-4):", "type", allow_empty=True)
        self._reach_row.add_field("slope:", "slope", allow_empty=True)
        form.addRow("Reaches:", self._reach_row)

        self._cent_row = _LayerRow(LayerFilterPoint)
        self._cent_row.add_field("id:", "id")
        self._cent_row.add_field("fi [0-1]:", "fi", allow_empty=True)
        form.addRow("Centroids:", self._cent_row)

        self._junc_row = _LayerRow(LayerFilterPoint)
        self._junc_row.add_field("id:", "id")
        self._junc_row.add_field("outlet(0/1):", "out")
        form.addRow("Confluences:", self._junc_row)

        self._basin_row = _LayerRow(LayerFilterPolygon)
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
        snap_row.addWidget(self._snap_tol)

        snap_row.addWidget(QLabel("  Project name:"))
        self._project_name = QLineEdit()
        self._project_name.setPlaceholderText("RORB Catchment")
        self._project_name.setMaxLength(69)
        snap_row.addWidget(self._project_name, 2)
        snap_row.addStretch()
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
        self._net_table.horizontalHeader().setSectionResizeMode(HeaderStretch)
        self._net_table.setEditTriggers(NoEditTriggers)
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
                li.setTextAlignment(AlignRightVCenter)
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

    # ---- .catg export -----------------------------------------------------
    def _on_export_catg(self):
        errors = self._validate_layers()
        if errors:
            QMessageBox.warning(self, "RORB Builder", "\n".join(errors)); return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export .catg", "", "RORB Control File (*.catg)")
        if not path: return
        try:
            from .core.catg_writer import write_catg
            nodes = self._build_nodes()
            title = self._project_name.text().strip() or "RORB Catchment"
            write_catg(nodes, path, title)
            self._log.append(f"Exported: {path}")
            QMessageBox.information(self, "RORB Builder", f"Exported:\n{path}")
        except Exception as e:
            import traceback
            self._log.append(f"Export error: {e}")
            QMessageBox.critical(self, "Export Failed", str(e) + "\n\n" + traceback.format_exc())

    def _build_nodes(self):
        """Build {int_id: node_dict} from centroid + confluence + basin layers."""
        basin_layer = self._basin_row.layer()
        basins = list(basin_layer.getFeatures()) if basin_layer else []

        nodes = {}
        id_fld = self._cent_row.field("id")
        fi_fld = self._cent_row.field("fi")
        for feat in self._cent_row.layer().getFeatures():
            raw_id = feat[id_fld]
            if raw_id is None or raw_id == '':
                continue
            geom = feat.geometry()
            if geom is None or geom.isNull():
                continue
            pt = geom.asPoint()
            fi = float(feat[fi_fld] or 0.0) if fi_fld else 0.0
            area_km2 = 0.0
            for b in basins:
                bg = b.geometry()
                if bg and not bg.isNull() and bg.contains(geom):
                    area_km2 = bg.area() / 1e6
                    break
            nodes[int(raw_id)] = {
                'x': pt.x(), 'y': pt.y(),
                'node_type': 1,
                'name': str(int(raw_id)),
                'area': area_km2,
                'fi': fi,
                'print_node': False,
                'print_code': 70,
                'location': '',
            }

        id_fld_j = self._junc_row.field("id")
        out_fld  = self._junc_row.field("out")
        for feat in self._junc_row.layer().getFeatures():
            raw_id = feat[id_fld_j]
            if raw_id is None or raw_id == '':
                continue
            geom = feat.geometry()
            if geom is None or geom.isNull():
                continue
            pt = geom.asPoint()
            is_outlet = bool(int(feat[out_fld] or 0)) if out_fld else False
            nodes[int(raw_id)] = {
                'x': pt.x(), 'y': pt.y(),
                'node_type': 0 if is_outlet else 3,
                'name': str(int(raw_id)),
                'area': 0.0,
                'fi': 0.0,
                'print_node': is_outlet,
                'print_code': 70,
                'location': str(int(raw_id)) if is_outlet else '',
            }

        if not nodes:
            raise ValueError("No nodes found. Check centroid and confluence layers.")
        return nodes

    # ---- Helpers ----------------------------------------------------------
    def _validate_layers(self):
        errors = []
        if not self._cent_row.layer():    errors.append("No centroid layer.")
        if not self._junc_row.layer():    errors.append("No confluence layer.")
        if not self._cent_row.field("id"):  errors.append("Centroid id field required.")
        if not self._junc_row.field("id"):  errors.append("Confluence id field required.")
        if not self._junc_row.field("out"): errors.append("Confluence outlet field required.")
        return errors
