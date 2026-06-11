import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon


class RorbQgisPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, 'RORB Builder', self.iface.mainWindow())
        self.action.setToolTip('Run RORB rainfall-runoff routing from shapefiles')
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&RORB', self.action)

    def unload(self):
        self.iface.removePluginMenu('&RORB', self.action)
        self.iface.removeToolBarIcon(self.action)
        del self.action

    def run(self):
        from .dialog import RorbModelDialog
        if self.dialog is None:
            self.dialog = RorbModelDialog(self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
