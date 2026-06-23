# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorDestination,
    QgsProcessingParameterCrs,
    QgsFeature,
    QgsField,
    QgsFields,
)
from ..compat import STRING, INT, FAST_INSERT, TYPE_POINT, WKB_POINT


class CreateConfluenceAlgorithm(QgsProcessingAlgorithm):
    """
    Create a confluence point layer with the required RORB fields (id, out).

    If an existing point layer is supplied the geometry is copied across;
    otherwise an empty template is created ready for manual digitising.
    """

    INPUT  = 'INPUT'
    CRS    = 'CRS'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        input_param = QgsProcessingParameterFeatureSource(
            self.INPUT,
            self.tr('Existing point layer to convert (optional)'),
            [TYPE_POINT],
            optional=True
        )
        self.addParameter(input_param)

        self.addParameter(
            QgsProcessingParameterCrs(
                self.CRS,
                self.tr('CRS (used when no input layer is given)'),
                defaultValue='EPSG:4326'
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Confluence layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        crs    = self.parameterAsCrs(parameters, self.CRS, context)

        fields = QgsFields()
        fields.append(QgsField('id',  STRING))
        fields.append(QgsField('out', INT))

        out_crs = source.sourceCrs() if source else crs

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, WKB_POINT, out_crs
        )

        if source:
            total = source.featureCount()
            for i, feat in enumerate(source.getFeatures()):
                out_val = 0
                if 'out' in [f.name() for f in feat.fields()]:
                    try:
                        out_val = int(feat['out']) if feat['out'] is not None else 0
                    except (ValueError, TypeError):
                        out_val = 0

                out = QgsFeature(fields)
                out.setGeometry(feat.geometry())
                out.setAttributes(['', out_val])
                sink.addFeature(out, FAST_INSERT)
                feedback.setProgress(int((i + 1) / total * 100) if total else 0)
        else:
            feedback.pushInfo(
                'No input layer provided — empty confluence layer created. '
                'Digitise confluence points into it, then run Auto Name Confluences.'
            )

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'create_confluence'

    def displayName(self):
        return self.tr('Create Confluence Layer')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Create RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Create a confluence point layer with the required RORB fields:\n\n"
            "  id  — confluence identifier (string, blank — filled by Auto Name Confluences)\n"
            "  out — outlet flag (integer, 0 = internal node, 1 = catchment outlet)\n\n"
            "Input layer (optional): if provided, geometry is copied and the 'out' field "
            "is preserved if it exists. If left empty, an empty template layer is created "
            "ready for manual digitising.\n\n"
            "Run Auto Name Confluences after digitising to assign letter IDs (a, b, c, …)."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CreateConfluenceAlgorithm()
