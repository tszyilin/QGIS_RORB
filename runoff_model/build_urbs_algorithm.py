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

__author__ = 'Lindsay Millard'
__date__ = '2025-08-20'
__copyright__ = '(C) 2025 by Tom Norman'

__revision__ = '$Format:%H$'

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingException,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterDefinition)
import os
import pyromb

from .custom_types.qvector_layer import (QVectorLayer,
                                          SnappedQVectorLayer,
                                          snap_reach_endpoints)


class BuildUrbsAlgorithm(QgsProcessingAlgorithm):
    """
    Build a URBS control vector file from input GIS layers.
    """

    IN_REACH = 'IN_REACH'
    IN_BASIN = 'IN_BASIN'
    IN_CENTROID = 'IN_CENTROID'
    IN_CONFLUENCE = 'IN_CONFLUENCE'
    OUTPUT = 'OUTPUT'
    SNAP_TOLERANCE = 'SNAP_TOLERANCE'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_REACH,
                self.tr('Reach layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_BASIN,
                self.tr('Basin layer'),
                [QgsProcessing.TypeVectorPolygon]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CENTROID,
                self.tr('Centroid layer'),
                [QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CONFLUENCE,
                self.tr('Confluence layer'),
                [QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                self.tr('URBS vector file (.vec)'),
                self.tr('URBS vector (*.vec)')
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

    @staticmethod
    def _check_fields(source, layer_label, required_fields):
        existing = {f.name() for f in source.fields()}
        missing = [f for f in required_fields if f not in existing]
        if missing:
            raise QgsProcessingException(
                f"{layer_label} layer is missing required field(s): "
                f"{', '.join(missing)}.\n"
                f"See the algorithm help (?) for full field requirements."
            )

    def processAlgorithm(self, parameters, context, feedback):
        reaches = self.parameterAsSource(parameters, self.IN_REACH, context)
        basins = self.parameterAsSource(parameters, self.IN_BASIN, context)
        centroids = self.parameterAsSource(parameters, self.IN_CENTROID, context)
        confluences = self.parameterAsSource(parameters, self.IN_CONFLUENCE, context)
        sink = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        tolerance = self.parameterAsDouble(parameters, self.SNAP_TOLERANCE, context)

        self._check_fields(reaches,     'Reach',      ['id', 't', 's'])
        self._check_fields(centroids,   'Centroid',   ['id', 'fi'])
        self._check_fields(confluences, 'Confluence', ['id', 'out'])

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

        model_name = os.path.splitext(os.path.basename(sink))[0]
        urbs_model = pyromb.URBS(model_name)
        urbs_vector = traveller.getVector(urbs_model)

        vec_content, cat_content = urbs_model.splitVector(urbs_vector)

        with open(sink, 'w') as f:
            f.write(vec_content)

        cat_file = os.path.splitext(sink)[0] + '.cat'
        with open(cat_file, 'w') as f:
            f.write(cat_content)

        feedback.pushInfo('Generated URBS files:')
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
        return ''

    def shortHelpString(self):
        return self.tr(
            "Build URBS model files (.vec + .cat) from GIS layers.\n\n"

            "━━ REQUIRED SHAPEFILE FIELDS ━━\n\n"

            "Reach layer (lines)\n"
            "  id  : unique reach name (text)\n"
            "  t   : reach type — 1 Natural, 2 Unlined channel,\n"
            "                      3 Lined channel, 4 Drowned (integer)\n"
            "  s   : slope in m/m (decimal, e.g. 0.005)\n\n"

            "Centroid layer (points)\n"
            "  id  : unique basin name — must match a basin polygon (text)\n"
            "  fi  : fraction impervious 0.0–1.0 (decimal)\n\n"

            "Basin layer (polygons)\n"
            "  No attribute fields required.\n"
            "  Area is calculated from the polygon geometry.\n\n"

            "Confluence layer (points)\n"
            "  id  : unique confluence name (text)\n"
            "  out : catchment outlet? 1 = yes, 0 = no (integer)\n"
            "        Exactly one confluence must have out = 1.\n\n"

            "━━ ADVANCED OPTIONS ━━\n\n"

            "Snap tolerance (map units, default 1.0)\n"
            "  Reach endpoints within this distance of a confluence or\n"
            "  centroid are snapped to it before processing. Set to 0\n"
            "  to disable. Increase if you still get topology errors.\n\n"

            "━━ OUTPUT ━━\n\n"
            "  .vec file: URBS command sequence (RAIN, ADD RAIN, STORE, GET...)\n"
            "  .cat file: subcatchment data in CSV format\n"
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return BuildUrbsAlgorithm()
