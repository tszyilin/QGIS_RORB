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
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from ..compat import INT, FAST_INSERT, TYPE_POLYGON


class AutoNameSubsAlgorithm(QgsProcessingAlgorithm):
    """Number subcatchment polygons from south to north using centroid y-coordinate."""

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Subcatchments layer'),
                [TYPE_POLYGON]
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Numbered subcatchments')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)

        # Build output fields: copy all input fields, add/replace integer 'id'
        in_fields = source.fields()
        out_fields = QgsFields()
        id_idx = in_fields.indexFromName('id')
        for field in in_fields:
            if field.name() != 'id':
                out_fields.append(field)
        out_fields.append(QgsField('id', INT))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, source.wkbType(), source.sourceCrs()
        )

        # Determine transform for centroid y calculation
        layer_crs = source.sourceCrs()
        if layer_crs.isGeographic():
            projected_crs = QgsCoordinateReferenceSystem('EPSG:3857')
            transform = QgsCoordinateTransform(layer_crs, projected_crs, QgsProject.instance())
        else:
            transform = None

        # Collect (centroid_y, feature) pairs
        features_with_y = []
        for feat in source.getFeatures():
            geom = feat.geometry()
            if transform:
                geom_proj = geom.__class__(geom)
                geom_proj.transform(transform)
                centroid_y = geom_proj.centroid().asPoint().y()
            else:
                centroid_y = geom.centroid().asPoint().y()
            features_with_y.append((centroid_y, feat))

        # Sort south to north (ascending y)
        features_with_y.sort(key=lambda x: x[0])

        total = len(features_with_y)
        for i, (_, feat) in enumerate(features_with_y):
            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(feat.geometry())
            # Copy all non-id attributes
            attrs = []
            for field in in_fields:
                if field.name() != 'id':
                    attrs.append(feat[field.name()])
            attrs.append(i + 1)  # id starts at 1
            new_feat.setAttributes(attrs)
            sink.addFeature(new_feat, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100))

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'auto_name_subs'

    def displayName(self):
        return self.tr('Auto Name Subcatchments (S to N)')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Prepare RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Number subcatchment polygons sequentially from south to north.\n\n"
            "The 'id' field is set to integers 1, 2, 3, ... with 1 being the "
            "southernmost polygon (lowest centroid y-coordinate).\n\n"
            "Run this before Auto Name Centroids, which uses these IDs to assign "
            "letter codes to centroid points."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return AutoNameSubsAlgorithm()
