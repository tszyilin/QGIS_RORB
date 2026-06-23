# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'
__revision__ = '$Format:%H$'

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms.build_rorb_algorithm import BuildRorbAlgorithm
from .algorithms.build_urbs_algorithm import BuildUrbsAlgorithm
from .algorithms.auto_name_subs_algorithm import AutoNameSubsAlgorithm
from .algorithms.auto_name_centroids_algorithm import AutoNameCentroidsAlgorithm
from .algorithms.auto_name_confluences_algorithm import AutoNameConfluencesAlgorithm
from .algorithms.auto_name_reaches_algorithm import AutoNameReachesAlgorithm
from .algorithms.check_rorb_links_algorithm import CheckRorbLinksAlgorithm
from .algorithms.cleanup_rorb_links_algorithm import CleanupRorbLinksAlgorithm
from .algorithms.create_centroid_algorithm import CreateCentroidAlgorithm
from .algorithms.create_confluence_algorithm import CreateConfluenceAlgorithm
from .algorithms.create_reach_algorithm import CreateReachAlgorithm


class RorbCatgProvider(QgsProcessingProvider):

    def __init__(self):
        QgsProcessingProvider.__init__(self)

    def unload(self):
        pass

    def loadAlgorithms(self):
        self.addAlgorithm(CreateCentroidAlgorithm())
        self.addAlgorithm(CreateConfluenceAlgorithm())
        self.addAlgorithm(CreateReachAlgorithm())
        self.addAlgorithm(AutoNameSubsAlgorithm())
        self.addAlgorithm(AutoNameCentroidsAlgorithm())
        self.addAlgorithm(AutoNameConfluencesAlgorithm())
        self.addAlgorithm(AutoNameReachesAlgorithm())
        self.addAlgorithm(CheckRorbLinksAlgorithm())
        self.addAlgorithm(CleanupRorbLinksAlgorithm())
        self.addAlgorithm(BuildRorbAlgorithm())
        self.addAlgorithm(BuildUrbsAlgorithm())

    def id(self):
        return 'rorb_catg'

    def name(self):
        return self.tr('QGIS RORB')

    def icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.png')
        return QIcon(icon_path)

    def longName(self):
        return self.name()
