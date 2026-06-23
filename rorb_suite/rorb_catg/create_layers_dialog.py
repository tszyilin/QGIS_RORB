# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton, QFrame,
)
from qgis.PyQt.QtCore import Qt  # noqa
from .compat import ALIGN_RIGHT, WIN_ON_TOP, HLINE
from qgis.PyQt.QtGui import QFont


class CreateLayersDialog(QDialog):
    """Launcher for the three Create RORB Layers processing algorithms."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('QGIS RORB — Create Layers')
        self.setMinimumWidth(340)
        self.setWindowFlags(self.windowFlags() | WIN_ON_TOP)
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        lbl = QLabel('Select a layer type to create:')
        f = QFont(); f.setBold(True)
        lbl.setFont(f)
        root.addWidget(lbl)

        self._add_btn(
            root,
            '  Create Centroid Layer',
            'Compute centroids from subcatchment polygons.\n'
            'Adds required fields:  id (str)  ·  fi (float)',
            '#c0392b',
            'rorb_catg:create_centroid',
        )

        self._add_btn(
            root,
            '  Create Confluence Layer',
            'Create a point layer for confluence nodes.\n'
            'Adds required fields:  id (str)  ·  out (int)',
            '#2471a3',
            'rorb_catg:create_confluence',
        )

        self._add_btn(
            root,
            '  Create Reach Layer',
            'Create a line layer for stream reaches.\n'
            'Adds required fields:  id (str)  ·  t (int)  ·  s (float)',
            '#1a7a1a',
            'rorb_catg:create_reach',
        )

        line = QFrame(); line.setFrameShape(HLINE)
        root.addWidget(line)

        btn_close = QPushButton('Close')
        btn_close.clicked.connect(self.close)
        root.addWidget(btn_close, alignment=ALIGN_RIGHT)

    def _add_btn(self, layout, label, tooltip, color, alg_id):
        btn = QPushButton(label)
        btn.setFixedHeight(52)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            f'QPushButton {{ background-color: {color}; color: white; '
            f'border-radius: 4px; font-size: 11pt; text-align: left; padding-left: 12px; }}'
            f'QPushButton:hover {{ background-color: {color}CC; }}'
        )
        btn.clicked.connect(lambda checked, a=alg_id: self._launch(a))
        layout.addWidget(btn)

    @staticmethod
    def _launch(alg_id):
        import processing
        processing.execAlgorithmDialog(alg_id)
