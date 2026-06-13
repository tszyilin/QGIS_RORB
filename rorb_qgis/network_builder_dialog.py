"""
RORB Network Builder.

Lets the user digitise RORB centroid, junction and reach layers directly
in QGIS with the correct attribute schema expected by the RORB Builder tab.

Layer schemas created
---------------------
RORB_Centroids  (Point)  : id, name, area_km2, fi, print_node
RORB_Junctions  (Point)  : id, name, is_outlet
RORB_Reaches    (Line)   : id, name, type, length_km, slope, translation, print_ud

All field names match what the RORB Builder dialog uses for its field combos.
"""

import traceback

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QTabWidget, QWidget,
    QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit,
    QCheckBox, QMessageBox, QSizePolicy,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor, QPixmap

from qgis.core import (
    QgsVectorLayer, QgsField, QgsFeature, QgsGeometry,
    QgsPointXY, QgsProject, QgsWkbTypes, QgsDistanceArea,
    QgsCoordinateReferenceSystem,
)
from qgis.gui import QgsMapTool, QgsMapToolEmitPoint, QgsRubberBand

try:
    from qgis.PyQt.QtCore import QVariant
except ImportError:
    from PyQt5.QtCore import QVariant


# ── Field schemas ──────────────────────────────────────────────────────────────

_CENTROID_FIELDS = [
    ('id',         QVariant.Int,    'Node ID'),
    ('name',       QVariant.String, 'Sub-area name'),
    ('area_km2',   QVariant.Double, 'Area (km²)'),
    ('fi',         QVariant.Double, 'Fraction impervious'),
    ('print_node', QVariant.Int,    'Print at node (0/1)'),
]

_JUNCTION_FIELDS = [
    ('id',        QVariant.Int,    'Junction ID'),
    ('name',      QVariant.String, 'Junction name'),
    ('is_outlet', QVariant.Int,    'Is outlet (0/1)'),
]

_REACH_FIELDS = [
    ('id',          QVariant.Int,    'Reach ID'),
    ('name',        QVariant.String, 'Reach name'),
    ('type',        QVariant.Int,    'Reach type (1–4)'),
    ('length_km',   QVariant.Double, 'Length (km)'),
    ('slope',       QVariant.Double, 'Slope (%)'),
    ('translation', QVariant.Int,    'Translation (time incs, 0=off)'),
    ('print_ud',    QVariant.Int,    'Print U/S and D/S (0/1)'),
]


def _make_layer(geometry_type, name, fields_spec, crs_authid='EPSG:4326'):
    """Create an in-memory vector layer with the given field schema."""
    uri   = f"{geometry_type}?crs={crs_authid}"
    layer = QgsVectorLayer(uri, name, 'memory')
    pr    = layer.dataProvider()
    for fname, ftype, _ in fields_spec:
        f = QgsField(fname, ftype)
        pr.addAttributes([f])
    layer.updateFields()
    return layer


def _next_id(layer, field='id'):
    """Return max(id)+1 for the layer, or 1 if empty."""
    ids = [f[field] for f in layer.getFeatures()
           if f[field] is not None and f[field] != NULL]
    return max(ids, default=0) + 1


try:
    from qgis.core import NULL
except ImportError:
    NULL = None


# ── Map tools ──────────────────────────────────────────────────────────────────

class _PointTool(QgsMapToolEmitPoint):
    """Emit a single map point on left-click, deactivate on right-click."""
    point_picked = pyqtSignal(object)   # QgsPointXY
    cancelled    = pyqtSignal()

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            pt = self.toMapCoordinates(e.pos())
            self.point_picked.emit(pt)
        elif e.button() == Qt.RightButton:
            self.cancelled.emit()


class _LineTool(QgsMapTool):
    """Click to add vertices, right-click to finish, Escape to cancel."""
    line_finished = pyqtSignal(object)   # QgsGeometry
    cancelled     = pyqtSignal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self._pts = []
        self._rb  = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._rb.setColor(QColor('#ef4444'))
        self._rb.setWidth(2)

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            pt = self.toMapCoordinates(e.pos())
            self._pts.append(pt)
            self._rb.addPoint(pt)
        elif e.button() == Qt.RightButton:
            if len(self._pts) >= 2:
                geom = QgsGeometry.fromPolylineXY(self._pts)
                self._reset()
                self.line_finished.emit(geom)
            else:
                self._reset()
                self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._reset()
            self.cancelled.emit()

    def canvasMoveEvent(self, e):
        if self._pts:
            pt = self.toMapCoordinates(e.pos())
            self._rb.movePoint(pt)

    def _reset(self):
        self._pts = []
        self._rb.reset(QgsWkbTypes.LineGeometry)

    def deactivate(self):
        self._reset()
        super().deactivate()


# ── Attribute dialogs ──────────────────────────────────────────────────────────

class CentroidDialog(QDialog):
    """Add Node (sub-area centroid) dialog — mirrors RORB's 'Add Node' UI."""

    def __init__(self, node_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Node")
        self.setMinimumWidth(420)
        self._build(node_id)

    def _build(self, node_id):
        lay = QVBoxLayout(self)

        # Header row
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Node Number"))
        num_lbl = QLineEdit(str(node_id)); num_lbl.setReadOnly(True)
        num_lbl.setFixedWidth(60)
        hdr.addWidget(num_lbl)
        hdr.addStretch()
        lay.addLayout(hdr)

        lay.addWidget(_hsep())

        tabs = QTabWidget()
        tabs.addTab(self._params_tab(), "Parameters")
        tabs.addTab(QWidget(), "Graphics")
        tabs.addTab(QWidget(), "Comments")
        lay.addWidget(tabs)

        btn = QHBoxLayout()
        ok  = QPushButton("OK");     ok.setDefault(True)
        can = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        can.clicked.connect(self.reject)
        btn.addStretch(); btn.addWidget(ok); btn.addWidget(can)
        lay.addLayout(btn)

    def _params_tab(self):
        w = QWidget(); form = QFormLayout(w)

        self._name  = QLineEdit()
        self._area  = QDoubleSpinBox()
        self._area.setRange(0, 99999); self._area.setDecimals(6)
        self._area.setSuffix("  km²")
        self._fi    = QDoubleSpinBox()
        self._fi.setRange(0, 1); self._fi.setDecimals(6)
        self._print = QCheckBox("Print at This Node")
        self._print.setChecked(True)
        self._ptype = QComboBox()
        self._ptype.addItem("1. Print calculated discharge (Code 7)", 7)

        form.addRow("Sub-area Name:", self._name)
        form.addRow("Area:", self._area)
        form.addRow("Fraction Impervious:", self._fi)
        form.addRow("", self._print)
        form.addRow("Print Type:", self._ptype)
        return w

    def values(self):
        return {
            'name':       self._name.text().strip(),
            'area_km2':   self._area.value(),
            'fi':         self._fi.value(),
            'print_node': 1 if self._print.isChecked() else 0,
        }


class JunctionDialog(QDialog):
    """Add Junction / Outlet node dialog."""

    def __init__(self, junc_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Junction")
        self.setMinimumWidth(360)
        self._build(junc_id)

    def _build(self, junc_id):
        lay = QVBoxLayout(self)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Junction Number"))
        num = QLineEdit(str(junc_id)); num.setReadOnly(True); num.setFixedWidth(60)
        hdr.addWidget(num); hdr.addStretch()
        lay.addLayout(hdr)

        lay.addWidget(_hsep())

        tabs = QTabWidget()
        tabs.addTab(self._params_tab(), "Parameters")
        tabs.addTab(QWidget(), "Graphics")
        tabs.addTab(QWidget(), "Comments")
        lay.addWidget(tabs)

        btn = QHBoxLayout()
        ok  = QPushButton("OK"); ok.setDefault(True)
        can = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        can.clicked.connect(self.reject)
        btn.addStretch(); btn.addWidget(ok); btn.addWidget(can)
        lay.addLayout(btn)

    def _params_tab(self):
        w = QWidget(); form = QFormLayout(w)
        self._name    = QLineEdit()
        self._outlet  = QCheckBox("This is the catchment outlet")
        form.addRow("Junction Name:", self._name)
        form.addRow("", self._outlet)
        return w

    def values(self):
        return {
            'name':      self._name.text().strip(),
            'is_outlet': 1 if self._outlet.isChecked() else 0,
        }


class ReachDialog(QDialog):
    """Reach Details dialog — mirrors RORB's 'Reach Details' UI."""

    def __init__(self, reach_id, length_km=0.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reach Details")
        self.setMinimumWidth(440)
        self._build(reach_id, length_km)

    def _build(self, reach_id, length_km):
        lay = QVBoxLayout(self)

        hdr = QFormLayout()
        num = QLineEdit(str(reach_id)); num.setReadOnly(True); num.setFixedWidth(60)
        self._rname = QLineEdit()
        hdr.addRow("Reach Number", num)
        hdr.addRow("Reach Name",   self._rname)
        lay.addLayout(hdr)

        lay.addWidget(_hsep())

        tabs = QTabWidget()
        tabs.addTab(self._params_tab(length_km), "Parameters")
        tabs.addTab(QWidget(), "Graphics")
        tabs.addTab(QWidget(), "Comments")
        lay.addWidget(tabs)

        btn = QHBoxLayout()
        ok  = QPushButton("OK"); ok.setDefault(True)
        can = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        can.clicked.connect(self.reject)
        btn.addStretch(); btn.addWidget(ok); btn.addWidget(can)
        lay.addLayout(btn)

    def _params_tab(self, length_km):
        w = QWidget(); form = QFormLayout(w)

        self._rtype = QComboBox()
        for txt, val in [("1. Natural", 1), ("2. Unlined channel", 2),
                         ("3. Lined channel", 3), ("4. Drowned outfall", 4)]:
            self._rtype.addItem(txt, val)

        self._length = QDoubleSpinBox()
        self._length.setRange(0, 99999); self._length.setDecimals(4)
        self._length.setSuffix("  kilometres"); self._length.setValue(length_km)

        self._slope = QDoubleSpinBox()
        self._slope.setRange(0, 100); self._slope.setDecimals(4)
        self._slope.setSuffix("  %")

        self._trans_chk = QCheckBox("Translation (Code 8)")
        self._trans_val = QSpinBox()
        self._trans_val.setRange(0, 9999); self._trans_val.setSuffix("  time increments")
        self._trans_val.setEnabled(False)
        self._trans_chk.stateChanged.connect(
            lambda s: self._trans_val.setEnabled(bool(s)))
        trans_row = QHBoxLayout()
        trans_row.addWidget(self._trans_chk)
        trans_row.addWidget(self._trans_val)
        trans_row.addStretch()

        self._print_ud = QCheckBox("Print U/S and D/S (Code +10)")

        form.addRow("Reach Type:", self._rtype)
        form.addRow("Length:",     self._length)
        form.addRow("Slope:",      self._slope)
        form.addRow("",            trans_row)
        form.addRow("",            self._print_ud)
        return w

    def values(self):
        trans = self._trans_val.value() if self._trans_chk.isChecked() else 0
        return {
            'name':        self._rname.text().strip(),
            'type':        self._rtype.currentData(),
            'length_km':   self._length.value(),
            'slope':       self._slope.value(),
            'translation': trans,
            'print_ud':    1 if self._print_ud.isChecked() else 0,
        }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hsep():
    from qgis.PyQt.QtWidgets import QFrame
    line = QFrame(); line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken); return line


def _calc_length_km(geom, layer_crs):
    """Calculate geometry length in km using ellipsoidal distance."""
    try:
        da = QgsDistanceArea()
        da.setSourceCrs(layer_crs, QgsProject.instance().transformContext())
        da.setEllipsoid(QgsProject.instance().ellipsoid() or 'WGS84')
        return da.measureLength(geom) / 1000.0
    except Exception:
        return geom.length() / 1000.0


# ── Main dialog ────────────────────────────────────────────────────────────────

class RorbNetworkBuilderDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface   = iface
        self.canvas  = iface.mapCanvas()
        self._prev_tool   = None
        self._node_tool   = None
        self._reach_tool  = None
        self.setWindowTitle("RORB Network Builder")
        self.setMinimumSize(480, 360)
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Layer setup
        setup_box = QGroupBox("Layers")
        gl = QFormLayout(setup_box)

        self._cent_lbl = QLabel("—"); self._cent_lbl.setStyleSheet("color:gray;")
        self._junc_lbl = QLabel("—"); self._junc_lbl.setStyleSheet("color:gray;")
        self._reach_lbl= QLabel("—"); self._reach_lbl.setStyleSheet("color:gray;")

        init_btn = QPushButton("Initialise / Load Layers")
        init_btn.clicked.connect(self._init_layers)

        gl.addRow("Centroids:",  self._cent_lbl)
        gl.addRow("Junctions:",  self._junc_lbl)
        gl.addRow("Reaches:",    self._reach_lbl)
        gl.addRow("",            init_btn)
        root.addWidget(setup_box)

        # Add buttons
        act_box = QGroupBox("Digitise")
        act_lay = QHBoxLayout(act_box)

        self._cent_btn  = QPushButton("➕  Add Centroid")
        self._junc_btn  = QPushButton("➕  Add Junction / Outlet")
        self._reach_btn = QPushButton("➕  Add Reach")
        for btn in (self._cent_btn, self._junc_btn, self._reach_btn):
            btn.setMinimumHeight(38)
            btn.setEnabled(False)
            act_lay.addWidget(btn)

        self._cent_btn.clicked.connect(self._start_add_centroid)
        self._junc_btn.clicked.connect(self._start_add_junction)
        self._reach_btn.clicked.connect(self._start_add_reach)
        root.addWidget(act_box)

        # Status
        self._status = QLabel("Initialise layers to begin.")
        self._status.setStyleSheet(
            "color:#374151;background:#f3f4f6;"
            "padding:6px;border:1px solid #d1d5db;border-radius:3px;")
        root.addWidget(self._status)

        hint = QLabel(
            "Reach: left-click to add vertices  ·  right-click to finish  ·  Esc to cancel")
        hint.setStyleSheet("color:gray;font-size:8pt;")
        root.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(QPushButton("Close", clicked=self.close))
        root.addLayout(btn_row)

    # ── Layer initialisation ──────────────────────────────────────────────────

    def _init_layers(self):
        proj = QgsProject.instance()
        crs  = self.canvas.mapSettings().destinationCrs()
        auth = crs.authid() or 'EPSG:4326'

        def _get_or_create(name, geom_type, fields_spec):
            existing = proj.mapLayersByName(name)
            if existing:
                return existing[0]
            layer = _make_layer(geom_type, name, fields_spec, auth)
            proj.addMapLayer(layer)
            return layer

        self._cent_layer  = _get_or_create(
            'RORB_Centroids', 'Point',      _CENTROID_FIELDS)
        self._junc_layer  = _get_or_create(
            'RORB_Junctions', 'Point',      _JUNCTION_FIELDS)
        self._reach_layer = _get_or_create(
            'RORB_Reaches',   'LineString', _REACH_FIELDS)

        chip = "color:#16a34a;font-weight:bold;"
        self._cent_lbl.setText(self._cent_layer.name())
        self._cent_lbl.setStyleSheet(chip)
        self._junc_lbl.setText(self._junc_layer.name())
        self._junc_lbl.setStyleSheet(chip)
        self._reach_lbl.setText(self._reach_layer.name())
        self._reach_lbl.setStyleSheet(chip)

        for btn in (self._cent_btn, self._junc_btn, self._reach_btn):
            btn.setEnabled(True)
        self._set_status("Layers ready. Click an Add button then click on the map.")

    # ── Centroid ──────────────────────────────────────────────────────────────

    def _start_add_centroid(self):
        self._node_tool = _PointTool(self.canvas)
        self._node_tool.point_picked.connect(self._on_centroid_picked)
        self._node_tool.cancelled.connect(self._restore_tool)
        self._save_and_set_tool(self._node_tool)
        self._set_status("Click on the map to place a sub-area centroid …")

    def _on_centroid_picked(self, pt):
        self._restore_tool()
        nid = _next_id(self._cent_layer)
        dlg = CentroidDialog(nid, self)
        if dlg.exec_() != QDialog.Accepted:
            self._set_status("Cancelled."); return
        vals = dlg.values()
        feat = QgsFeature(self._cent_layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(pt))
        feat['id']         = nid
        feat['name']       = vals['name'] or f"SA{nid}"
        feat['area_km2']   = vals['area_km2']
        feat['fi']         = vals['fi']
        feat['print_node'] = vals['print_node']
        self._cent_layer.dataProvider().addFeature(feat)
        self._cent_layer.triggerRepaint()
        self._set_status(f"Centroid {nid} added.  Click again to add another.")
        self._start_add_centroid()

    # ── Junction ──────────────────────────────────────────────────────────────

    def _start_add_junction(self):
        self._node_tool = _PointTool(self.canvas)
        self._node_tool.point_picked.connect(self._on_junction_picked)
        self._node_tool.cancelled.connect(self._restore_tool)
        self._save_and_set_tool(self._node_tool)
        self._set_status("Click on the map to place a junction / outlet …")

    def _on_junction_picked(self, pt):
        self._restore_tool()
        jid = _next_id(self._junc_layer)
        dlg = JunctionDialog(jid, self)
        if dlg.exec_() != QDialog.Accepted:
            self._set_status("Cancelled."); return
        vals = dlg.values()
        feat = QgsFeature(self._junc_layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(pt))
        feat['id']        = jid
        feat['name']      = vals['name'] or f"J{jid}"
        feat['is_outlet'] = vals['is_outlet']
        self._junc_layer.dataProvider().addFeature(feat)
        self._junc_layer.triggerRepaint()
        label = "Outlet" if vals['is_outlet'] else "Junction"
        self._set_status(f"{label} {jid} added.  Click again to add another.")
        self._start_add_junction()

    # ── Reach ─────────────────────────────────────────────────────────────────

    def _start_add_reach(self):
        self._reach_tool = _LineTool(self.canvas)
        self._reach_tool.line_finished.connect(self._on_reach_finished)
        self._reach_tool.cancelled.connect(self._restore_tool)
        self._save_and_set_tool(self._reach_tool)
        self._set_status(
            "Click to add reach vertices.  Right-click to finish.  Esc to cancel.")

    def _on_reach_finished(self, geom):
        self._restore_tool()
        rid        = _next_id(self._reach_layer)
        length_km  = _calc_length_km(geom, self._reach_layer.crs())
        dlg = ReachDialog(rid, length_km, self)
        if dlg.exec_() != QDialog.Accepted:
            self._set_status("Cancelled."); return
        vals = dlg.values()
        feat = QgsFeature(self._reach_layer.fields())
        feat.setGeometry(geom)
        feat['id']          = rid
        feat['name']        = vals['name'] or f"R{rid}"
        feat['type']        = vals['type']
        feat['length_km']   = vals['length_km']
        feat['slope']       = vals['slope']
        feat['translation'] = vals['translation']
        feat['print_ud']    = vals['print_ud']
        self._reach_layer.dataProvider().addFeature(feat)
        self._reach_layer.triggerRepaint()
        self._set_status(
            f"Reach {rid} added ({vals['length_km']:.3f} km).  "
            "Click again to add another.")
        self._start_add_reach()

    # ── Tool management ───────────────────────────────────────────────────────

    def _save_and_set_tool(self, tool):
        self._prev_tool = self.canvas.mapTool()
        self.canvas.setMapTool(tool)

    def _restore_tool(self):
        if self._prev_tool:
            self.canvas.setMapTool(self._prev_tool)
        else:
            self.canvas.unsetMapTool(self.canvas.mapTool())
        self._set_status("Ready.")

    def _set_status(self, msg):
        self._status.setText(msg)

    def closeEvent(self, e):
        self._restore_tool()
        super().closeEvent(e)
