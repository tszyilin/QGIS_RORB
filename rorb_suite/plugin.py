import os

from qgis.core import QgsApplication
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QSize

_HERE = os.path.dirname(__file__)


class RorbSuitePlugin:
    TOOLBAR_NAME = 'RORB Tools'

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.toolbar = None
        self._results_dialog = None

    def initProcessing(self):
        from .rorb_catg.rorb_catg_provider import RorbCatgProvider
        self.provider = RorbCatgProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self._build_toolbar()

    def _build_toolbar(self):
        from .rorb_qgis.plugin import _peak_icon

        self.toolbar = self.iface.mainWindow().addToolBar(self.TOOLBAR_NAME)
        self.toolbar.setObjectName('RORBToolsToolbar')
        self.toolbar.setIconSize(QSize(24, 24))

        # Button 1 — Create RORB Layers
        action_create = QAction(
            QIcon(os.path.join(_HERE, 'rorb_catg', 'icon_create.svg')),
            'Create RORB Layers',
            self.iface.mainWindow(),
        )
        action_create.setToolTip('Create RORB layer templates (centroid, confluence, reach)')
        action_create.triggered.connect(self._show_create_layers)
        self.toolbar.addAction(action_create)

        # Button 2 — Build RORB .catg
        action_catg = QAction(
            QIcon(os.path.join(_HERE, 'rorb_catg', 'icon_catg.svg')),
            'Build RORB .catg',
            self.iface.mainWindow(),
        )
        action_catg.setToolTip('Name layers → check links → build .cat / .catg')
        action_catg.triggered.connect(self._show_pipeline)
        self.toolbar.addAction(action_catg)

        # Button 3 — RORB Results Viewer
        action_viewer = QAction(
            _peak_icon(),
            'RORB Results Viewer',
            self.iface.mainWindow(),
        )
        action_viewer.setToolTip('Browse .out files, plot hydrographs, export critical events')
        action_viewer.triggered.connect(self._show_viewer)
        self.toolbar.addAction(action_viewer)

    def _show_create_layers(self):
        from .rorb_catg.create_layers_dialog import CreateLayersDialog
        dlg = CreateLayersDialog(self.iface.mainWindow())
        dlg.show()

    def _show_pipeline(self):
        from .rorb_catg.pipeline_dialog import RorbPipelineDialog
        dlg = RorbPipelineDialog(self.iface, self.iface.mainWindow())
        dlg.show()

    def _show_viewer(self):
        from .rorb_qgis.results_dialog import RorbResultsDialog
        from .rorb_qgis.compat import RightDockWidgetArea
        if self._results_dialog is None:
            self._results_dialog = RorbResultsDialog(self.iface.mainWindow())
            self.iface.addDockWidget(RightDockWidgetArea, self._results_dialog)
            self._results_dialog.setFloating(True)
        self._results_dialog.show()
        self._results_dialog.raise_()

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.toolbar:
            self.toolbar.clear()
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar = None
        if self._results_dialog:
            self.iface.removeDockWidget(self._results_dialog)
            self._results_dialog.deleteLater()
            self._results_dialog = None
