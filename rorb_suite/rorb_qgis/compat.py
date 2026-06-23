"""Qt 5 (QGIS 3.x) / Qt 6 (QGIS 4.x) compatibility shims.

Import from here instead of using Qt.SomeFlag directly so only this file
needs updating when the Qt enum namespace changes between QGIS versions.
"""

from qgis.PyQt.QtCore import Qt, PYQT_VERSION_STR

PYQT6 = int(PYQT_VERSION_STR.split('.')[0]) >= 6

# ── Alignment ──────────────────────────────────────────────────────────────────
if PYQT6:
    AlignRight   = Qt.AlignmentFlag.AlignRight
    AlignLeft    = Qt.AlignmentFlag.AlignLeft
    AlignVCenter = Qt.AlignmentFlag.AlignVCenter
    AlignCenter  = Qt.AlignmentFlag.AlignCenter
    AlignHCenter = Qt.AlignmentFlag.AlignHCenter
else:
    AlignRight   = Qt.AlignRight
    AlignLeft    = Qt.AlignLeft
    AlignVCenter = Qt.AlignVCenter
    AlignCenter  = Qt.AlignCenter
    AlignHCenter = Qt.AlignHCenter

AlignRightVCenter = AlignRight | AlignVCenter

# ── Item data roles ────────────────────────────────────────────────────────────
if PYQT6:
    UserRole = Qt.ItemDataRole.UserRole
else:
    UserRole = Qt.UserRole

# ── Item flags ─────────────────────────────────────────────────────────────────
if PYQT6:
    ItemIsEnabled       = Qt.ItemFlag.ItemIsEnabled
    ItemIsUserCheckable = Qt.ItemFlag.ItemIsUserCheckable
else:
    ItemIsEnabled       = Qt.ItemIsEnabled
    ItemIsUserCheckable = Qt.ItemIsUserCheckable

# ── Check state ────────────────────────────────────────────────────────────────
if PYQT6:
    Checked   = Qt.CheckState.Checked
    Unchecked = Qt.CheckState.Unchecked
else:
    Checked   = Qt.Checked
    Unchecked = Qt.Unchecked

# ── Dock areas ────────────────────────────────────────────────────────────────
if PYQT6:
    RightDockWidgetArea = Qt.DockWidgetArea.RightDockWidgetArea
    AllDockWidgetAreas  = Qt.DockWidgetArea.AllDockWidgetAreas
else:
    RightDockWidgetArea = Qt.RightDockWidgetArea
    AllDockWidgetAreas  = Qt.AllDockWidgetAreas

# ── Orientation ────────────────────────────────────────────────────────────────
if PYQT6:
    Horizontal = Qt.Orientation.Horizontal
    Vertical   = Qt.Orientation.Vertical
else:
    Horizontal = Qt.Horizontal
    Vertical   = Qt.Vertical

# ── Context menu policy ────────────────────────────────────────────────────────
if PYQT6:
    CustomContextMenu = Qt.ContextMenuPolicy.CustomContextMenu
else:
    CustomContextMenu = Qt.CustomContextMenu

# ── Mouse buttons ──────────────────────────────────────────────────────────────
if PYQT6:
    LeftButton  = Qt.MouseButton.LeftButton
    RightButton = Qt.MouseButton.RightButton
else:
    LeftButton  = Qt.LeftButton
    RightButton = Qt.RightButton

# ── Keys ──────────────────────────────────────────────────────────────────────
if PYQT6:
    Key_Escape = Qt.Key.Key_Escape
else:
    Key_Escape = Qt.Key_Escape

# ── Pen / brush / colours ─────────────────────────────────────────────────────
if PYQT6:
    NoPen       = Qt.PenStyle.NoPen
    NoBrush     = Qt.BrushStyle.NoBrush
    RoundCap    = Qt.PenCapStyle.RoundCap
    transparent = Qt.GlobalColor.transparent
else:
    NoPen       = Qt.NoPen
    NoBrush     = Qt.NoBrush
    RoundCap    = Qt.RoundCap
    transparent = Qt.transparent

# ── Painter render hints ──────────────────────────────────────────────────────
from qgis.PyQt.QtGui import QPainter
if PYQT6:
    Antialiasing = QPainter.RenderHint.Antialiasing
else:
    Antialiasing = QPainter.Antialiasing

# ── Widget table / header enums ───────────────────────────────────────────────
from qgis.PyQt.QtWidgets import QAbstractItemView, QHeaderView, QFormLayout, QDialog

if PYQT6:
    NoEditTriggers        = QAbstractItemView.EditTrigger.NoEditTriggers
    SelectRows            = QAbstractItemView.SelectionBehavior.SelectRows
    HeaderStretch         = QHeaderView.ResizeMode.Stretch
    AllNonFixedFieldsGrow = QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
    DialogAccepted        = QDialog.DialogCode.Accepted
else:
    NoEditTriggers        = QAbstractItemView.NoEditTriggers
    SelectRows            = QAbstractItemView.SelectRows
    HeaderStretch         = QHeaderView.Stretch
    AllNonFixedFieldsGrow = QFormLayout.AllNonFixedFieldsGrow
    DialogAccepted        = QDialog.Accepted

# ── QFrame shapes / shadows ───────────────────────────────────────────────────
from qgis.PyQt.QtWidgets import QFrame

if PYQT6:
    FrameHLine  = QFrame.Shape.HLine
    FrameSunken = QFrame.Shadow.Sunken
else:
    FrameHLine  = QFrame.HLine
    FrameSunken = QFrame.Sunken

# ── QgsField types (QVariant in Qt5, QMetaType in Qt6) ───────────────────────
try:
    from qgis.PyQt.QtCore import QVariant
    FieldTypeInt    = QVariant.Int
    FieldTypeString = QVariant.String
    FieldTypeDouble = QVariant.Double
except AttributeError:
    try:
        from qgis.PyQt.QtCore import QMetaType
        FieldTypeInt    = QMetaType.Type.Int
        FieldTypeString = QMetaType.Type.QString
        FieldTypeDouble = QMetaType.Type.Double
    except Exception:
        FieldTypeInt    = int
        FieldTypeString = str
        FieldTypeDouble = float

# ── QGIS WkbTypes ─────────────────────────────────────────────────────────────
try:
    from qgis.core import QgsWkbTypes
    WkbLineGeometry = QgsWkbTypes.LineGeometry
except AttributeError:
    try:
        from qgis.core import Qgis
        WkbLineGeometry = Qgis.GeometryType.Line
    except Exception:
        WkbLineGeometry = 1

# ── QGIS layer filter flags ───────────────────────────────────────────────────
try:
    from qgis.core import QgsMapLayerProxyModel
    LayerFilterPoint   = QgsMapLayerProxyModel.PointLayer
    LayerFilterLine    = QgsMapLayerProxyModel.LineLayer
    LayerFilterPolygon = QgsMapLayerProxyModel.PolygonLayer
except AttributeError:
    try:
        from qgis.core import Qgis
        LayerFilterPoint   = Qgis.LayerFilter.PointLayer
        LayerFilterLine    = Qgis.LayerFilter.LineLayer
        LayerFilterPolygon = Qgis.LayerFilter.PolygonLayer
    except Exception:
        LayerFilterPoint = LayerFilterLine = LayerFilterPolygon = None

# ── Matplotlib canvas (Qt5 or Qt6 backend) ────────────────────────────────────
def _load_mpl():
    for backend in ('matplotlib.backends.backend_qtagg',
                    'matplotlib.backends.backend_qt5agg'):
        try:
            import importlib
            mod = importlib.import_module(backend)
            from matplotlib.figure import Figure
            return mod.FigureCanvasQTAgg, Figure
        except Exception:
            continue
    return None, None

FigureCanvas, Figure = _load_mpl()
HAS_MPL = FigureCanvas is not None
