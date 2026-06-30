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
        self._run_dialog = None

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

        # Button 2 — Build .catg & Run RORB
        action_catg = QAction(
            QIcon(os.path.join(_HERE, 'rorb_catg', 'icon_catg.svg')),
            'Build RORB .catg',
            self.iface.mainWindow(),
        )
        action_catg.setToolTip('Build .catg / run RORB ensemble')
        action_catg.triggered.connect(self._show_run_rorb)
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

    def _show_run_rorb(self):
        from .rorb_catg.run_rorb_dialog import RorbRunDialog
        if self._run_dialog is None or not self._run_dialog.isVisible():
            self._run_dialog = RorbRunDialog(
                self.iface,
                parent=self.iface.mainWindow(),
                on_open_results=self._open_results_folder,
            )
        self._run_dialog.show()
        self._run_dialog.raise_()

    def _show_viewer(self):
        from .rorb_qgis.results_dialog import RorbResultsDialog
        from .rorb_qgis.compat import RightDockWidgetArea
        from qgis.PyQt.QtCore import QTimer
        if self._results_dialog is None:
            self._results_dialog = RorbResultsDialog(self.iface.mainWindow())
            self.iface.addDockWidget(RightDockWidgetArea, self._results_dialog)
            # Defer setFloating to avoid a segfault on QGIS 3.38+ / newer Qt builds
            # where calling setFloating immediately after addDockWidget crashes.
            QTimer.singleShot(0, lambda: self._results_dialog.setFloating(True))
        self._results_dialog.show()
        self._results_dialog.raise_()

    def _open_results_folder(self, folder):
        self._show_viewer()
        name = os.path.basename(os.path.normpath(folder)) or 'RORB run'
        self._results_dialog.add_scenario(name, folder)

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.toolbar:
            self.toolbar.clear()
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar = None
        if self._run_dialog:
            self._run_dialog.deleteLater()
            self._run_dialog = None
        if self._results_dialog:
            self.iface.removeDockWidget(self._results_dialog)
            self._results_dialog.deleteLater()
            self._results_dialog = None
