# -*- coding: utf-8 -*-

"""
/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

__revision__ = '$Format:%H$'

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterDefinition)
import pyromb
from .custom_types.qvector_layer import (QVectorLayer,
                                         SnappedQVectorLayer,
                                         snap_reach_endpoints)


class BuildRorbAlgorithm(QgsProcessingAlgorithm):
    """
    Build a RORB control vector from a GIS representation.

    The plugin depends on python library pyromb.
    """

    OUTPUT = 'OUTPUT'
    IN_REACH = 'IN_REACH'
    IN_BASIN = 'IN_BASIN'
    IN_CENTROID = 'IN_CENTROID'
    IN_CONFLUENCE = 'IN_CONFLUENCE'
    SNAP_TOLERANCE = 'SNAP_TOLERANCE'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_REACH,
                self.tr('Reach'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_BASIN,
                self.tr('Basin'),
                [QgsProcessing.TypeVectorPolygon]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CENTROID,
                self.tr('Centroid'),
                [QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CONFLUENCE,
                self.tr('Confluence'),
                [QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                self.tr('Control File'),
                "Control Vector (*.cat)"
            )
        )

        snap_param = QgsProcessingParameterNumber(
            self.SNAP_TOLERANCE,
            self.tr('Snap tolerance (map units, 0 = disabled)'),
            QgsProcessingParameterNumber.Double,
            defaultValue=1.0,
            minValue=0.0,
            optional=True
        )
        snap_param.setFlags(
            snap_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced
        )
        self.addParameter(snap_param)

    def processAlgorithm(self, parameters, context, feedback):
        reaches = self.parameterAsSource(parameters, self.IN_REACH, context)
        basins = self.parameterAsSource(parameters, self.IN_BASIN, context)
        centroids = self.parameterAsSource(parameters, self.IN_CENTROID, context)
        confluences = self.parameterAsSource(parameters, self.IN_CONFLUENCE, context)
        sink = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        tolerance = self.parameterAsDouble(parameters, self.SNAP_TOLERANCE, context)

        reach_features = list(reaches.getFeatures())
        confluence_features = list(confluences.getFeatures())
        centroid_features = list(centroids.getFeatures())

        if tolerance > 0:
            feedback.pushInfo(f'Snapping reach endpoints within {tolerance} map units...')
            snapped_geoms = snap_reach_endpoints(
                reach_features,
                [confluence_features, centroid_features],
                tolerance
            )
            snapped_count = sum(
                1 for orig, snapped in zip(
                    [[(v.x(), v.y()) for v in f.geometry().vertices()] for f in reach_features],
                    snapped_geoms
                )
                if orig[0] != snapped[0] or orig[-1] != snapped[-1]
            )
            feedback.pushInfo(f'  {snapped_count} reach endpoint(s) snapped.')
            reach_vector = SnappedQVectorLayer(reach_features, snapped_geoms)
        else:
            reach_vector = QVectorLayer(reach_features)

        basin_vector = QVectorLayer(basins)
        centroid_vector = QVectorLayer(centroid_features)
        confluence_vector = QVectorLayer(confluence_features)

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
        return ''

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return BuildRorbAlgorithm()

    def shortHelpString(self):
        return self.tr(
            "Build RORB model files from GIS layers representing catchment "
            "reaches, basins, centroids, and confluences.\n\n"
            "Input layers:\n"
            "- Reach layer: Line features representing stream reaches\n"
            "- Basin layer: Polygon features representing catchment basins\n"
            "- Centroid layer: Point features representing basin centroids\n"
            "- Confluence layer: Point features representing stream confluences\n\n"
            "Advanced options:\n"
            "- Snap tolerance: reach endpoints within this distance (map units) "
            "of a confluence or centroid will be snapped to it before processing. "
            "Set to 0 to disable. Default is 1.0.\n\n"
            "The algorithm generates one file:\n"
            "- .cat file: RORB control vector\n\n"
        )
