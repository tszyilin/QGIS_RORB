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
from ..compat import STRING, INT, DOUBLE, FAST_INSERT, TYPE_LINE, WKB_LINE

_STYLES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'styles')

# RORB reach type codes (Table 5-1 in RORB manual)
_REACH_TYPE_INFO = (
    "Reach type codes (t field):\n"
    "  1 = Natural / irregular cross-section\n"
    "  2 = Drowned (tidal or backwater affected)\n"
    "  3 = Pipe or lined channel\n"
    "  4 = Broad-crested weir\n"
    "  5 = Sharp-crested weir\n"
    "  6 = Orifice\n"
    "  7 = User-defined storage-discharge"
)


class CreateReachAlgorithm(QgsProcessingAlgorithm):
    """
    Create a reach line layer with the required RORB fields (id, t, s).

    If an existing line layer is supplied the geometry is copied across and
    any existing t / s values are preserved; otherwise an empty template is
    created ready for manual digitising.
    """

    INPUT  = 'INPUT'
    CRS    = 'CRS'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        input_param = QgsProcessingParameterFeatureSource(
            self.INPUT,
            self.tr('Existing line layer to convert (optional)'),
            [TYPE_LINE],
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
                self.tr('Reach layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        crs    = self.parameterAsCrs(parameters, self.CRS, context)

        fields = QgsFields()
        fields.append(QgsField('id', STRING))
        fields.append(QgsField('t',  INT))
        fields.append(QgsField('s',  DOUBLE))

        out_crs  = source.sourceCrs() if source else crs
        out_type = source.wkbType()   if source else WKB_LINE

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            fields, out_type, out_crs
        )
        self._dest_id = dest_id

        if source:
            in_fields = source.fields()
            in_names  = [f.name() for f in in_fields]
            total = source.featureCount()

            for i, feat in enumerate(source.getFeatures()):
                t_val = 1
                if 't' in in_names:
                    try:
                        t_val = int(feat['t']) if feat['t'] is not None else 1
                    except (ValueError, TypeError):
                        t_val = 1

                s_val = 0.0
                if 's' in in_names:
                    try:
                        s_val = float(feat['s']) if feat['s'] is not None else 0.0
                    except (ValueError, TypeError):
                        s_val = 0.0

                out = QgsFeature(fields)
                out.setGeometry(feat.geometry())
                out.setAttributes(['', t_val, s_val])
                sink.addFeature(out, FAST_INSERT)
                feedback.setProgress(int((i + 1) / total * 100) if total else 0)
        else:
            feedback.pushInfo(
                'No input layer provided — empty reach layer created. '
                'Digitise reaches into it, then run Auto Name Reaches.'
            )

        return {self.OUTPUT: dest_id}

    def postProcessAlgorithm(self, context, feedback):
        layer = QgsProcessingUtils.mapLayerFromString(self._dest_id, context)
        if layer:
            qml = os.path.join(_STYLES_DIR, 'reaches.qml')
            if os.path.isfile(qml):
                layer.loadNamedStyle(qml)
                layer.triggerRepaint()
        return {}

    def name(self):
        return 'create_reach'

    def displayName(self):
        return self.tr('Create Reach Layer')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Create RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Create a reach line layer with the required RORB fields:\n\n"
            "  id — reach identifier (string, blank — filled by Auto Name Reaches)\n"
            "  t  — reach type (integer, default 1)\n"
            "  s  — slope (float, default 0.0)\n\n"
            f"{_REACH_TYPE_INFO}\n\n"
            "Input layer (optional): if provided, geometry is copied and existing "
            "t / s values are preserved. If left empty, a blank template is created "
            "ready for manual digitising.\n\n"
            "Run Auto Name Reaches after digitising to assign fromNode_toNode IDs."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CreateReachAlgorithm()
