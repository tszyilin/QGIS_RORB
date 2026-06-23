# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorDestination,
    QgsFeature,
    QgsField,
    QgsFields,
)
from ..compat import STRING, DOUBLE, FAST_INSERT, TYPE_POLYGON, WKB_POINT


class CreateCentroidAlgorithm(QgsProcessingAlgorithm):
    """
    Compute the centroid of each subcatchment polygon and output a point
    layer with the required RORB centroid fields (id, fi).
    """

    INPUT  = 'INPUT'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Subcatchment polygon layer'),
                [TYPE_POLYGON]
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Centroid layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)

        fields = QgsFields()
        fields.append(QgsField('id', STRING))
        fields.append(QgsField('fi', DOUBLE))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, WKB_POINT, source.sourceCrs()
        )

        total = source.featureCount()
        for i, feat in enumerate(source.getFeatures()):
            centroid = feat.geometry().centroid()
            out = QgsFeature(fields)
            out.setGeometry(centroid)
            out.setAttributes(['', 0.0])
            sink.addFeature(out, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100) if total else 0)

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'create_centroid'

    def displayName(self):
        return self.tr('Create Centroid Layer')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Create RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Compute the centroid point of each subcatchment polygon and create "
            "a point layer with the required RORB centroid fields:\n\n"
            "  id  — centroid identifier (string, blank — filled by Auto Name Centroids)\n"
            "  fi  — fraction impervious (float, default 0.0)\n\n"
            "Run Auto Name Centroids after this step to assign letter IDs (A, B, C, …)."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CreateCentroidAlgorithm()
