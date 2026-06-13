import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from qgis.PyQt.QtCore import Qt


def _letter_icon(letter, bg_color):
    """Create a simple 24×24 coloured square icon with a white letter."""
    px = QPixmap(24, 24)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(bg_color))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(0, 0, 24, 24, 5, 5)
    p.setPen(QColor('white'))
    f = QFont(); f.setBold(True); f.setPointSize(11)
    p.setFont(f)
    p.drawText(px.rect(), Qt.AlignCenter, letter)
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
            _letter_icon('B', '#2563eb'), 'RORB Builder', self.iface.mainWindow())
        self.action.setToolTip('Run RORB rainfall-runoff routing from shapefiles')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&RORB', self.action)

        self.action_results = QAction(
            _letter_icon('R', '#16a34a'), 'RORB Results Viewer', self.iface.mainWindow())
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
