import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon


class RorbQgisPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action         = None
        self.action_results = None
        self.dialog         = None
        self.results_dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, 'RORB Builder', self.iface.mainWindow())
        self.action.setToolTip('Run RORB rainfall-runoff routing from shapefiles')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&RORB', self.action)

        self.action_results = QAction(icon, 'RORB Results Viewer', self.iface.mainWindow())
        self.action_results.setToolTip(
            'Browse a folder of RORB CSV outputs, plot hydrographs, and export critical events')
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
