import os

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QSize

_HERE = os.path.dirname(__file__)


class RorbRunPlugin:
    TOOLBAR_NAME = 'RORB Run'

    def __init__(self, iface):
        self.iface = iface
        self.toolbar = None
        self._dlg = None
        self._stm_dlg = None

    def initGui(self):
        self.toolbar = self.iface.mainWindow().addToolBar(self.TOOLBAR_NAME)
        self.toolbar.setObjectName('RORBRunToolbar')
        self.toolbar.setIconSize(QSize(24, 24))

        action_run = QAction(
            QIcon(os.path.join(_HERE, 'icon_run.svg')),
            'Run RORB',
            self.iface.mainWindow(),
        )
        action_run.setToolTip('Run RORB_CMD.exe against a .catg / .stm pair')
        action_run.triggered.connect(self._show_run)
        self.toolbar.addAction(action_run)

        action_stm = QAction(
            QIcon(os.path.join(_HERE, 'icon_catg.svg')),
            'Generate STM',
            self.iface.mainWindow(),
        )
        action_stm.setToolTip('Generate .stm storm files from ARR2016 data or a custom time series')
        action_stm.triggered.connect(self._show_stm_generator)
        self.toolbar.addAction(action_stm)

    def _show_run(self):
        from .run_rorb_dialog import RorbRunDialog
        self._dlg = RorbRunDialog(self.iface, self.iface.mainWindow())
        self._dlg.show()

    def _show_stm_generator(self):
        from .stm_generator_dialog import StmGeneratorDialog
        self._stm_dlg = StmGeneratorDialog(self.iface.mainWindow())
        self._stm_dlg.show()

    def unload(self):
        if self.toolbar:
            self.toolbar.clear()
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar = None
