# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'
__revision__ = '$Format:%H$'

import os
import sys
import inspect

from qgis.core import QgsApplication
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QSize

from .rorb_catg_provider import RorbCatgProvider

cmd_folder = os.path.split(inspect.getfile(inspect.currentframe()))[0]

if cmd_folder not in sys.path:
    sys.path.insert(0, cmd_folder)

_HERE = os.path.dirname(__file__)


class RorbCatgPlugin(object):

    TOOLBAR_NAME = 'QGIS RORB'

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.toolbar = None
        self.action_create = None
        self.action_pipeline = None

    def initProcessing(self):
        self.provider = RorbCatgProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self._build_toolbar()

    def _build_toolbar(self):
        self.toolbar = self.iface.mainWindow().addToolBar(self.TOOLBAR_NAME)
        self.toolbar.setObjectName('QGISRORBToolbar')
        self.toolbar.setIconSize(QSize(24, 24))

        # ── Button 1: Create RORB Layers  (+) ────────────────────────────────
        self.action_create = QAction(
            QIcon(os.path.join(_HERE, 'icon_create.svg')),
            'Create RORB Layers',
            self.iface.mainWindow()
        )
        self.action_create.setToolTip(
            'Create RORB layer templates\n'
            '(centroid, confluence, reach)'
        )
        self.action_create.triggered.connect(self._show_create_layers)
        self.toolbar.addAction(self.action_create)

        # ── Button 2: Build .catg  (catchment) ───────────────────────────────
        self.action_pipeline = QAction(
            QIcon(os.path.join(_HERE, 'icon_catg.svg')),
            'Build RORB .catg',
            self.iface.mainWindow()
        )
        self.action_pipeline.setToolTip(
            'RORB Pipeline\n'
            'Name layers → check → build .cat / .catg'
        )
        self.action_pipeline.triggered.connect(self._show_pipeline)
        self.toolbar.addAction(self.action_pipeline)

    def _show_create_layers(self):
        from .create_layers_dialog import CreateLayersDialog
        dlg = CreateLayersDialog(self.iface.mainWindow())
        dlg.show()

    def _show_pipeline(self):
        from .pipeline_dialog import RorbPipelineDialog
        dlg = RorbPipelineDialog(self.iface, self.iface.mainWindow())
        dlg.show()

    def unload(self):
        QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.toolbar:
            self.toolbar.clear()
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar = None
