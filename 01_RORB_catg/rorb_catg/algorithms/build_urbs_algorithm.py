# -*- coding: utf-8 -*-

__author__ = 'Lindsay Millard'
__date__ = '2025-08-20'
__copyright__ = '(C) 2025 by Tom Norman'
__revision__ = '$Format:%H$'

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination)
from ..compat import TYPE_LINE, TYPE_POLYGON, TYPE_POINT
try:
    import pyromb
except ImportError:
    import sys
    sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'vendor')))
    import pyromb
from ..custom_types.qvector_layer import QVectorLayer


class BuildUrbsAlgorithm(QgsProcessingAlgorithm):
    """Build a URBS control vector from GIS catchment layers."""

    IN_REACH = 'IN_REACH'
    IN_BASIN = 'IN_BASIN'
    IN_CENTROID = 'IN_CENTROID'
    IN_CONFLUENCE = 'IN_CONFLUENCE'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_REACH,
                self.tr('Reach layer'),
                [TYPE_LINE]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_BASIN,
                self.tr('Basin layer'),
                [TYPE_POLYGON]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CENTROID,
                self.tr('Centroid layer'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CONFLUENCE,
                self.tr('Confluence layer'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                self.tr('URBS vector file (.vec)'),
                self.tr('URBS vector (*.vec)')
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

        model_name = os.path.splitext(os.path.basename(sink))[0]
        urbs_model = pyromb.URBS(model_name)
        urbs_vector = traveller.getVector(urbs_model)

        vec_content, cat_content = urbs_model.splitVector(urbs_vector)

        with open(sink, 'w') as f:
            f.write(vec_content)

        cat_file = os.path.splitext(sink)[0] + '.cat'
        with open(cat_file, 'w') as f:
            f.write(cat_content)

        feedback.pushInfo(f'Generated URBS files:')
        feedback.pushInfo(f'  Vector file: {sink}')
        feedback.pushInfo(f'  Catchment data file: {cat_file}')

        return {self.OUTPUT: sink}

    def name(self):
        return 'Build URBS'

    def displayName(self):
        return self.tr(self.name())

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Build Models'

    def shortHelpString(self):
        return self.tr(
            "Build URBS model files (.vec + .cat) from GIS catchment layers.\n\n"
            "Inputs:\n"
            "- Reach: line features with length and slope attributes\n"
            "- Basin: polygon features with area and imperviousness\n"
            "- Centroid: point features representing basin centroids\n"
            "- Confluence: point features representing stream confluences\n\n"
            "Outputs two files:\n"
            "- .vec file: URBS command sequence\n"
            "- .cat file: subcatchment data in CSV format\n\n"
            "Requires the pyromb library (pyromb>=0.3)."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return BuildUrbsAlgorithm()
