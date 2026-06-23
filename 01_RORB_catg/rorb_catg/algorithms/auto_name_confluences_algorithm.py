# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

import string

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorDestination,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsFeatureSink,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from ..compat import STRING, INT, FAST_INSERT, TYPE_POINT


def generate_lowercase_ids(num_ids):
    """Generate lowercase letter IDs: a, b, ..., z, aa, ab, ..."""
    letters = string.ascii_lowercase
    base = len(letters)
    ids = []
    for i in range(num_ids):
        n = i
        result = ''
        while True:
            result = letters[n % base] + result
            n = n // base - 1
            if n < 0:
                break
        ids.append(result)
    return ids


class AutoNameConfluencesAlgorithm(QgsProcessingAlgorithm):
    """Assign lowercase letter IDs to confluence points ordered south to north."""

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Confluences layer'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Named confluences')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)

        in_fields = source.fields()
        out_fields = QgsFields()
        for field in in_fields:
            if field.name() not in ('id', 'out'):
                out_fields.append(field)
        out_fields.append(QgsField('id', STRING))
        out_fields.append(QgsField('out', INT))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, source.wkbType(), source.sourceCrs()
        )

        # Determine transform for y-coordinate sorting
        layer_crs = source.sourceCrs()
        if layer_crs.isGeographic():
            projected_crs = QgsCoordinateReferenceSystem('EPSG:28351')
            transform = QgsCoordinateTransform(layer_crs, projected_crs, QgsProject.instance())
        else:
            transform = None

        # Collect (y, feature) pairs
        features_with_y = []
        for feat in source.getFeatures():
            geom = feat.geometry()
            if transform:
                geom_proj = geom.__class__(geom)
                geom_proj.transform(transform)
                y = geom_proj.asPoint().y()
            else:
                y = geom.asPoint().y()
            features_with_y.append((y, feat))

        # Sort south to north
        features_with_y.sort(key=lambda x: x[0])

        id_list = generate_lowercase_ids(len(features_with_y))
        total = len(features_with_y)

        for i, (_, feat) in enumerate(features_with_y):
            # Get existing 'out' value or default to 0
            out_val = 0
            if 'out' in [f.name() for f in in_fields]:
                out_raw = feat['out']
                try:
                    out_val = int(out_raw) if out_raw is not None else 0
                except (ValueError, TypeError):
                    out_val = 0

            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(feat.geometry())
            attrs = []
            for field in in_fields:
                if field.name() not in ('id', 'out'):
                    attrs.append(feat[field.name()])
            attrs.append(id_list[i])
            attrs.append(out_val)
            new_feat.setAttributes(attrs)
            sink.addFeature(new_feat, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100) if total else 0)

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'auto_name_confluences'

    def displayName(self):
        return self.tr('Auto Name Confluences (S to N)')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Prepare RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Assign lowercase letter IDs (a, b, c, ...) to confluence points ordered "
            "from south to north by their y-coordinate.\n\n"
            "Also ensures an 'out' (outlet flag) integer field exists, defaulting to 0."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return AutoNameConfluencesAlgorithm()
