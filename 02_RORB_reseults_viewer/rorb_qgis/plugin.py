import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from qgis.PyQt.QtCore import Qt

from .compat import (
    transparent, NoPen, NoBrush, RoundCap, Antialiasing, RightDockWidgetArea,
)


def _peak_icon():
    """Viewer icon: stylised hydrograph (rising limb, peak, recession)."""
    from qgis.PyQt.QtGui import QPen, QPainterPath
    px = QPixmap(24, 24); px.fill(transparent)
    p = QPainter(px); p.setRenderHint(Antialiasing)
    green = QColor('#16a34a')
    p.setBrush(green); p.setPen(NoPen)
    p.drawRoundedRect(0, 0, 24, 24, 4, 4)
    # hydrograph curve
    path = QPainterPath()
    path.moveTo(2, 20)
    path.cubicTo(6, 20, 8, 4, 12, 4)    # rising limb
    path.cubicTo(15, 4, 16, 14, 22, 18) # recession
    pen = QPen(QColor('white'), 2.0)
    pen.setCapStyle(RoundCap)
    p.setPen(pen); p.setBrush(NoBrush)
    p.drawPath(path)
    # peak dot
    p.setBrush(QColor('#fbbf24')); p.setPen(NoPen)
    p.drawEllipse(10, 2, 5, 5)
    p.end()
    return QIcon(px)


class RorbQgisPlugin:
    def __init__(self, iface):
        self.iface          = iface
        self.action_results = None
        self.results_dialog = None

    def initGui(self):
        self.action_results = QAction(
            _peak_icon(), 'RORB QGIS', self.iface.mainWindow())
        self.action_results.setToolTip(
            'Browse a folder of RORB .out files, plot hydrographs, export critical events')
        self.action_results.triggered.connect(self.run_results)
        self.iface.addToolBarIcon(self.action_results)
        self.iface.addPluginToMenu('&RORB', self.action_results)

    def unload(self):
        self.iface.removePluginMenu('&RORB', self.action_results)
        self.iface.removeToolBarIcon(self.action_results)
        if self.results_dialog:
            self.iface.removeDockWidget(self.results_dialog)
            self.results_dialog.deleteLater()
            self.results_dialog = None
        del self.action_results

    def run_results(self):
        from .results_dialog import RorbResultsDialog
        if self.results_dialog is None:
            self.results_dialog = RorbResultsDialog(self.iface.mainWindow())
            self.iface.addDockWidget(RightDockWidgetArea, self.results_dialog)
            self.results_dialog.setFloating(True)
        self.results_dialog.show()
        self.results_dialog.raise_()
