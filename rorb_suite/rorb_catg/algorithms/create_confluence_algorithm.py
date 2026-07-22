# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorDestination,
    QgsProcessingParameterCrs,
    QgsProcessingUtils,
    QgsFeature,
    QgsField,
    QgsFields,
)
from ..compat import STRING, INT, FAST_INSERT, TYPE_POINT, WKB_POINT

_STYLES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'styles')


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
        fields.append(QgsField('id',         STRING))
        fields.append(QgsField('out',        INT))
        fields.append(QgsField('print_node', INT))     # 0 = no print, 1 = print at this node
        fields.append(QgsField('print_code', STRING))  # '7', '7.1', or '7.2'
        fields.append(QgsField('node_name',  STRING))  # optional location label written after the print instruction

        out_crs = source.sourceCrs() if source else crs

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, WKB_POINT, out_crs
        )
        self._dest_id = dest_id

        if source:
            total = source.featureCount()
            existing_names = {f.name() for f in source.fields()}
            for i, feat in enumerate(source.getFeatures()):
                def _int(fname, default=0):
                    if fname in existing_names:
                        try:
                            return int(feat[fname]) if feat[fname] is not None else default
                        except (ValueError, TypeError):
                            pass
                    return default

                def _str(fname, default=''):
                    if fname in existing_names:
                        v = feat[fname]
                        return str(v) if v is not None else default
                    return default

                out = QgsFeature(fields)
                out.setGeometry(feat.geometry())
                out.setAttributes([
                    '',                        # id (blank — filled by Auto Name Confluences)
                    _int('out'),               # outlet flag
                    _int('print_node'),        # print at this node
                    _str('print_code'),        # '7', '7.1', '7.2'
                    _str('node_name'),         # optional location label
                ])
                sink.addFeature(out, FAST_INSERT)
                feedback.setProgress(int((i + 1) / total * 100) if total else 0)
        else:
            feedback.pushInfo(
                'No input layer provided — empty confluence layer created. '
                'Digitise confluence points into it, then run Auto Name Confluences.'
            )

        return {self.OUTPUT: dest_id}

    def postProcessAlgorithm(self, context, feedback):
        layer = QgsProcessingUtils.mapLayerFromString(self._dest_id, context)
        if layer:
            qml = os.path.join(_STYLES_DIR, 'confluence.qml')
            if os.path.isfile(qml):
                layer.loadNamedStyle(qml)
                layer.triggerRepaint()
        return {}

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
            "  id         — confluence identifier (blank — filled by Auto Name Confluences)\n"
            "  out        — outlet flag (0 = internal node, 1 = catchment outlet)\n"
            "  print_node — print flag (0 = no print, 1 = insert print instruction)\n"
            "  print_code — print instruction: '7' (discharge), '7.1' (disc+actual), '7.2' (dummy gauge)\n"
            "  node_name  — optional location label written after the print instruction in the .catg\n\n"
            "Input layer (optional): if provided, geometry is copied and existing field "
            "values are preserved. If left empty, an empty template layer is created."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CreateConfluenceAlgorithm()
