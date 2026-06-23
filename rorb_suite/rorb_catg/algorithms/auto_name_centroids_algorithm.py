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
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsProject,
)
from ..compat import STRING, DOUBLE, FAST_INSERT, TYPE_POINT, TYPE_POLYGON


def id_to_letter(subcatchment_id):
    """Convert an integer subcatchment id to uppercase letter(s): 1→A, 2→B, ..., 27→AA, ..."""
    try:
        index = int(subcatchment_id) - 1
        letters = string.ascii_uppercase
        base = len(letters)
        if index < base:
            return letters[index]
        result = ''
        while index >= 0:
            result = letters[index % base] + result
            index = index // base - 1
        return result
    except (ValueError, TypeError):
        return None


class AutoNameCentroidsAlgorithm(QgsProcessingAlgorithm):
    """Assign letter IDs to centroid points based on which subcatchment they fall in."""

    IN_SUBCATCHMENTS = 'IN_SUBCATCHMENTS'
    IN_CENTROIDS = 'IN_CENTROIDS'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_SUBCATCHMENTS,
                self.tr('Subcatchments layer (with numeric id field)'),
                [TYPE_POLYGON]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CENTROIDS,
                self.tr('Centroids layer'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Named centroids')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        subs_source = self.parameterAsSource(parameters, self.IN_SUBCATCHMENTS, context)
        cent_source = self.parameterAsSource(parameters, self.IN_CENTROIDS, context)

        # Build output fields: copy centroid fields, add/replace 'id' (string) and 'fi' (float)
        in_fields = cent_source.fields()
        out_fields = QgsFields()
        for field in in_fields:
            if field.name() not in ('id', 'fi'):
                out_fields.append(field)
        out_fields.append(QgsField('id', STRING))
        out_fields.append(QgsField('fi', DOUBLE))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, cent_source.wkbType(), cent_source.sourceCrs()
        )

        # Load all subcatchment features, transforming to centroid CRS if needed
        subs_crs = subs_source.sourceCrs()
        cent_crs = cent_source.sourceCrs()
        transform = None
        if subs_crs != cent_crs:
            transform = QgsCoordinateTransform(subs_crs, cent_crs, QgsProject.instance())

        # Build spatial index and dict of subcatchment features (in centroid CRS)
        subs_index = QgsSpatialIndex()
        subs_dict = {}
        for sub_feat in subs_source.getFeatures():
            geom = sub_feat.geometry()
            if transform:
                geom.transform(transform)
            # Store a copy with the transformed geometry
            f = QgsFeature(sub_feat)
            f.setGeometry(geom)
            subs_index.insertFeature(f)
            subs_dict[sub_feat.id()] = f

        total = cent_source.featureCount()
        multiple_centroids = []

        for i, cent_feat in enumerate(cent_source.getFeatures()):
            point_geom = cent_feat.geometry()
            candidate_ids = subs_index.intersects(point_geom.boundingBox())
            matched_sub_id = None
            match_count = 0

            for fid in candidate_ids:
                sub_feat = subs_dict[fid]
                if sub_feat.geometry().contains(point_geom):
                    match_count += 1
                    if matched_sub_id is None:
                        matched_sub_id = sub_feat['id']

            if match_count > 1:
                multiple_centroids.append(cent_feat.id())

            letter_id = id_to_letter(matched_sub_id) if matched_sub_id is not None else None

            # Get existing fi value or default to 0.0
            fi_val = 0.0
            if 'fi' in [f.name() for f in in_fields]:
                fi_raw = cent_feat['fi']
                try:
                    fi_val = float(fi_raw) if fi_raw is not None else 0.0
                except (ValueError, TypeError):
                    fi_val = 0.0

            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(cent_feat.geometry())
            attrs = []
            for field in in_fields:
                if field.name() not in ('id', 'fi'):
                    attrs.append(cent_feat[field.name()])
            attrs.append(str(letter_id) if letter_id else '')
            attrs.append(fi_val)
            new_feat.setAttributes(attrs)
            sink.addFeature(new_feat, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100) if total else 0)

        if multiple_centroids:
            feedback.pushWarning(
                f'Warning: {len(multiple_centroids)} centroid(s) fell inside more than one '
                f'subcatchment polygon. Only the first match was used.'
            )

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'auto_name_centroids'

    def displayName(self):
        return self.tr('Auto Name Centroids (S to N)')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Prepare RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Assign letter IDs (A, B, C, ...) to centroid points based on which "
            "subcatchment polygon each centroid falls within.\n\n"
            "The subcatchments layer must already have a numeric 'id' field "
            "(from Auto Name Subcatchments). Subcatchment 1 → 'A', 2 → 'B', etc.\n\n"
            "Also ensures a 'fi' (fraction impervious) field exists, defaulting to 0.0."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return AutoNameCentroidsAlgorithm()
