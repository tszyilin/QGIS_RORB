# -*- coding: utf-8 -*-
"""
QGIS 3.x / 4.x  +  PyQt5 / PyQt6 compatibility shims.

Strategy: always try the OLD (QGIS 3.x / PyQt5) form first — QGIS maintains
backward compatibility for most APIs.  Fall back to the new form only when
the old one genuinely raises AttributeError.  Never gate on version numbers
alone, because minor 4.x builds may still expose old-style attributes.
"""

# ── Field type constants ──────────────────────────────────────────────────────
# Old: QVariant.Int / Double / String  (PyQt5, QGIS 3.x)
# New: QMetaType.Type.Int / Double / QString  (PyQt6, QGIS 4.x)
# NOTE: QMetaType exists in PyQt5 too, so we cannot gate on its presence.
try:
    from qgis.PyQt.QtCore import QVariant
    INT    = QVariant.Int
    DOUBLE = QVariant.Double
    STRING = QVariant.String
    # Verify the values are actually usable (in PyQt6 QVariant may exist but be empty)
    _test = QVariant(INT)
    del _test
except Exception:
    from qgis.PyQt.QtCore import QMetaType
    INT    = QMetaType.Type.Int
    DOUBLE = QMetaType.Type.Double
    STRING = QMetaType.Type.QString

# ── WKB geometry type constants ───────────────────────────────────────────────
try:
    from qgis.core import QgsWkbTypes
    WKB_POINT       = QgsWkbTypes.Point
    WKB_LINE        = QgsWkbTypes.LineString
    WKB_POLYGON     = QgsWkbTypes.Polygon
    WKB_MULTILINE   = QgsWkbTypes.MultiLineString
    WKB_MULTIPOINT  = QgsWkbTypes.MultiPoint
    WKB_LINE_GEOMETRY = QgsWkbTypes.LineGeometry

    def wkb_geometry_type(wkb_type):
        return QgsWkbTypes.geometryType(wkb_type)

except AttributeError:
    from qgis.core import Qgis
    WKB_POINT       = Qgis.WkbType.Point
    WKB_LINE        = Qgis.WkbType.LineString
    WKB_POLYGON     = Qgis.WkbType.Polygon
    WKB_MULTILINE   = Qgis.WkbType.MultiLineString
    WKB_MULTIPOINT  = Qgis.WkbType.MultiPoint
    WKB_LINE_GEOMETRY = Qgis.GeometryType.Line

    def wkb_geometry_type(wkb_type):
        return Qgis.geometryType(wkb_type)

# ── QgsFeatureSink flags ──────────────────────────────────────────────────────
from qgis.core import QgsFeatureSink
try:
    FAST_INSERT = QgsFeatureSink.FastInsert          # QGIS 3.x + 4.x compat
except AttributeError:
    FAST_INSERT = QgsFeatureSink.SinkFlag.FastInsert  # strict PyQt6 enum

# ── Processing source type constants ─────────────────────────────────────────
from qgis.core import QgsProcessing
try:
    TYPE_POINT   = QgsProcessing.TypeVectorPoint
    TYPE_LINE    = QgsProcessing.TypeVectorLine
    TYPE_POLYGON = QgsProcessing.TypeVectorPolygon
except AttributeError:
    TYPE_POINT   = QgsProcessing.SourceType.TypeVectorPoint
    TYPE_LINE    = QgsProcessing.SourceType.TypeVectorLine
    TYPE_POLYGON = QgsProcessing.SourceType.TypeVectorPolygon

# ── Map layer proxy model filter constants ────────────────────────────────────
from qgis.core import QgsMapLayerProxyModel
try:
    LAYER_POINT   = QgsMapLayerProxyModel.PointLayer
    LAYER_LINE    = QgsMapLayerProxyModel.LineLayer
    LAYER_POLYGON = QgsMapLayerProxyModel.PolygonLayer
except AttributeError:
    LAYER_POINT   = QgsMapLayerProxyModel.Filter.PointLayer
    LAYER_LINE    = QgsMapLayerProxyModel.Filter.LineLayer
    LAYER_POLYGON = QgsMapLayerProxyModel.Filter.PolygonLayer

# ── QFrame shape constants ────────────────────────────────────────────────────
from qgis.PyQt.QtWidgets import QFrame
try:
    HLINE = QFrame.HLine
    VLINE = QFrame.VLine
    int(HLINE)
except (AttributeError, TypeError):
    HLINE = QFrame.Shape.HLine
    VLINE = QFrame.Shape.VLine

# ── Qt alignment / window flags ───────────────────────────────────────────────
from qgis.PyQt.QtCore import Qt
try:
    ALIGN_RIGHT   = Qt.AlignRight
    ALIGN_CENTER  = Qt.AlignCenter
    ALIGN_LEFT    = Qt.AlignLeft
    ALIGN_HCENTER = Qt.AlignHCenter
    ALIGN_VCENTER = Qt.AlignVCenter
    WIN_ON_TOP    = Qt.WindowStaysOnTopHint
    # Verify (PyQt6 enums raise TypeError when used incorrectly)
    int(ALIGN_RIGHT)
except (AttributeError, TypeError):
    ALIGN_RIGHT   = Qt.AlignmentFlag.AlignRight
    ALIGN_CENTER  = Qt.AlignmentFlag.AlignCenter
    ALIGN_LEFT    = Qt.AlignmentFlag.AlignLeft
    ALIGN_HCENTER = Qt.AlignmentFlag.AlignHCenter
    ALIGN_VCENTER = Qt.AlignmentFlag.AlignVCenter
    WIN_ON_TOP    = Qt.WindowType.WindowStaysOnTopHint

# ── QgsVectorFileWriter helper ────────────────────────────────────────────────
from qgis.core import (
    QgsVectorFileWriter,
    QgsCoordinateTransformContext,
)

def make_shapefile_writer(path, fields, wkb_type, crs):
    """
    Return an open QgsVectorFileWriter for an ESRI Shapefile.
    Uses QgsVectorFileWriter.create() (available since QGIS 3.10),
    compatible with both QGIS 3.x and 4.x.
    """
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName   = 'ESRI Shapefile'
    options.fileEncoding = 'UTF-8'
    return QgsVectorFileWriter.create(
        path, fields, wkb_type, crs,
        QgsCoordinateTransformContext(), options
    )
