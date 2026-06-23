# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'
__revision__ = '$Format:%H$'

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination)
from ..compat import TYPE_LINE, TYPE_POLYGON, TYPE_POINT
try:
    import pyromb
except ImportError:
    import sys, os as _os
    sys.path.insert(0, _os.path.normpath(_os.path.join(_os.path.dirname(__file__), '..', 'vendor')))
    import pyromb
from ..custom_types.qvector_layer import QVectorLayer


class BuildRorbAlgorithm(QgsProcessingAlgorithm):
    """Build a RORB control vector from GIS catchment layers."""

    OUTPUT = 'OUTPUT'
    IN_REACH = 'IN_REACH'
    IN_BASIN = 'IN_BASIN'
    IN_CENTROID = 'IN_CENTROID'
    IN_CONFLUENCE = 'IN_CONFLUENCE'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_REACH,
                self.tr('Reach'),
                [TYPE_LINE]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_BASIN,
                self.tr('Basin'),
                [TYPE_POLYGON]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CENTROID,
                self.tr('Centroid'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CONFLUENCE,
                self.tr('Confluence'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                self.tr('Control File (.cat or .catg)'),
                "RORB Graphical (*.catg);;Control Vector (*.cat)"
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        reaches = self.parameterAsSource(parameters, self.IN_REACH, context)
        basins = self.parameterAsSource(parameters, self.IN_BASIN, context)
        centroids = self.parameterAsSource(parameters, self.IN_CENTROID, context)
        confluences = self.parameterAsSource(parameters, self.IN_CONFLUENCE, context)
        sink = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        reach_vector = QVectorLayer(reaches)
        basin_vector = QVectorLayer(basins)
        centroid_vector = QVectorLayer(centroids)
        confluence_vector = QVectorLayer(confluences)

        builder = pyromb.Builder()
        tr = builder.reach(reach_vector)
        tc = builder.confluence(confluence_vector)
        tb = builder.basin(centroid_vector, basin_vector)

        catchment = pyromb.Catchment(tc, tb, tr)
        catchment.connect()
        traveller = pyromb.Traveller(catchment)

        feedback.setProgress(1)

        with open(sink, 'w') as f:
            f.write(traveller.getVector(pyromb.RORB()))

        return {self.OUTPUT: sink}

    def name(self):
        return 'Build RORB'

    def displayName(self):
        return self.tr(self.name())

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Build Models'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return BuildRorbAlgorithm()

    def shortHelpString(self):
        return self.tr(
            "Build a RORB control vector (.cat) from GIS catchment layers.\n\n"
            "Inputs:\n"
            "- Reach: line features with length and slope attributes\n"
            "- Basin: polygon features with area and imperviousness\n"
            "- Centroid: point features representing basin centroids\n"
            "- Confluence: point features representing stream confluences\n\n"
            "Requires the pyromb library (pyromb>=0.3)."
        )
