import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from qgis.PyQt.QtCore import Qt


def _network_icon():
    """Builder icon: catchment network (nodes + directed edges)."""
    from qgis.PyQt.QtCore import QPointF
    px = QPixmap(24, 24); px.fill(Qt.transparent)
    p = QPainter(px); p.setRenderHint(QPainter.Antialiasing)
    white = QColor('white')
    blue  = QColor('#2563eb')
    # background
    p.setBrush(blue); p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, 24, 24, 4, 4)
    # nodes: 3 sub-areas top, 1 outlet bottom
    nodes = [(5, 5), (12, 5), (19, 5), (12, 19)]
    # edges: sub-areas → outlet
    p.setPen(white); p.pen().setWidth(1)
    from qgis.PyQt.QtGui import QPen
    pen = QPen(white, 1.4); p.setPen(pen)
    for nx, ny in nodes[:3]:
        p.drawLine(nx, ny + 3, nodes[3][0], nodes[3][1] - 3)
    # draw nodes
    p.setBrush(white); p.setPen(Qt.NoPen)
    for nx, ny in nodes[:3]:
        p.drawEllipse(nx - 3, ny - 3, 6, 6)
    p.setBrush(QColor('#fbbf24')); p.drawEllipse(9, 16, 6, 6)
    p.end()
    return QIcon(px)


def _peak_icon():
    """Viewer icon: stylised hydrograph (rising limb, peak, recession)."""
    from qgis.PyQt.QtGui import QPen, QPainterPath
    px = QPixmap(24, 24); px.fill(Qt.transparent)
    p = QPainter(px); p.setRenderHint(QPainter.Antialiasing)
    green = QColor('#16a34a')
    p.setBrush(green); p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, 24, 24, 4, 4)
    # hydrograph curve
    path = QPainterPath()
    path.moveTo(2, 20)
    path.cubicTo(6, 20, 8, 4, 12, 4)    # rising limb
    path.cubicTo(15, 4, 16, 14, 22, 18) # recession
    pen = QPen(QColor('white'), 2.0)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen); p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    # peak dot
    p.setBrush(QColor('#fbbf24')); p.setPen(Qt.NoPen)
    p.drawEllipse(10, 2, 5, 5)
    p.end()
    return QIcon(px)


class RorbQgisPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action         = None
        self.action_results = None
        self.dialog         = None
        self.results_dialog = None

    def initGui(self):
        self.action = QAction(
            _network_icon(), 'RORB Builder', self.iface.mainWindow())
        self.action.setToolTip('Run RORB rainfall-runoff routing from shapefiles')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&RORB', self.action)

        self.action_results = QAction(
            _peak_icon(), 'RORB Results Viewer', self.iface.mainWindow())
        self.action_results.setToolTip(
            'Browse a folder of RORB .out files, plot hydrographs, export critical events')
        self.action_results.triggered.connect(self.run_results)
        self.iface.addToolBarIcon(self.action_results)
        self.iface.addPluginToMenu('&RORB', self.action_results)

    def unload(self):
        self.iface.removePluginMenu('&RORB', self.action)
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu('&RORB', self.action_results)
        self.iface.removeToolBarIcon(self.action_results)
        del self.action
        del self.action_results

    def run(self):
        from .dialog import RorbModelDialog
        if self.dialog is None:
            self.dialog = RorbModelDialog(self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def run_results(self):
        from .results_dialog import RorbResultsDialog
        if self.results_dialog is None:
            self.results_dialog = RorbResultsDialog(self.iface.mainWindow())
        self.results_dialog.show()
        self.results_dialog.raise_()
        self.results_dialog.activateWindow()
