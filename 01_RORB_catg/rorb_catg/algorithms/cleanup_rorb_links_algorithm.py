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
from ..compat import FAST_INSERT, TYPE_LINE


class CleanupRorbLinksAlgorithm(QgsProcessingAlgorithm):
    """Replace negative slope values in the 's' field of a reach layer with 0."""

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Reaches layer (with s field)'),
                [TYPE_LINE]
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Cleaned reaches')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)

        in_fields = source.fields()
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            in_fields, source.wkbType(), source.sourceCrs()
        )

        s_idx = in_fields.indexFromName('s')
        if s_idx < 0:
            feedback.pushWarning("Field 's' not found in the reaches layer. No changes made.")

        total = source.featureCount()
        fixed_count = 0

        for i, feat in enumerate(source.getFeatures()):
            new_feat = QgsFeature(feat)

            if s_idx >= 0:
                s_val = feat['s']
                try:
                    s_float = float(s_val) if s_val is not None else 0.0
                except (ValueError, TypeError):
                    s_float = 0.0

                if s_float < 0:
                    attrs = new_feat.attributes()
                    attrs[s_idx] = 0.0
                    new_feat.setAttributes(attrs)
                    fixed_count += 1

            sink.addFeature(new_feat, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100) if total else 0)

        if s_idx >= 0:
            if fixed_count > 0:
                feedback.pushInfo(
                    f'Replaced {fixed_count} negative slope value(s) with 0.0.'
                )
            else:
                feedback.pushInfo('No negative slope values found. No changes made.')

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'cleanup_rorb_links'

    def displayName(self):
        return self.tr('Cleanup RORB Links (Fix Negative Slopes)')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Check / Clean RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Replace negative values in the 's' (slope) field of a reach layer with 0.\n\n"
            "Negative slopes can arise from DEM artefacts or digitising direction and will "
            "cause errors in RORB. Reports how many values were corrected."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CleanupRorbLinksAlgorithm()
