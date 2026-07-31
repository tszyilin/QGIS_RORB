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
        # Also store polygon centroid points for nearest-neighbour fallback
        subs_index = QgsSpatialIndex()
        subs_dict = {}       # qgis fid → QgsFeature (transformed)
        sub_id_to_fid = {}   # 'id' field value → qgis fid
        sub_centroids = {}   # 'id' field value → polygon centroid QgsPointXY
        for sub_feat in subs_source.getFeatures():
            geom = sub_feat.geometry()
            if transform:
                geom.transform(transform)
            f = QgsFeature(sub_feat)
            f.setGeometry(geom)
            subs_index.insertFeature(f)
            subs_dict[sub_feat.id()] = f
            sid = sub_feat['id']
            sub_id_to_fid[sid] = sub_feat.id()
            sub_centroids[sid] = geom.centroid().asPoint()

        # --- Pass 1: match each centroid to a polygon ---
        # poly_to_cents: polygon 'id' → list of (cent_feat, fi_val)
        # cent_to_poly:  cent qgis fid → matched polygon 'id' (or None)
        cent_features = list(cent_source.getFeatures())
        poly_to_cents = {}   # polygon id → [cent_feat, ...]
        cent_to_poly = {}    # cent fid → polygon id or None
        multiple_centroids = []

        for cent_feat in cent_features:
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
            cent_to_poly[cent_feat.id()] = matched_sub_id
            if matched_sub_id is not None:
                poly_to_cents.setdefault(matched_sub_id, []).append(cent_feat)

        # --- Pass 2: resolve duplicates ---
        # For polygons with >1 centroid, keep the first; re-assign the rest to
        # the nearest polygon that currently has no centroid.
        all_sub_ids = set()
        for sub_feat in subs_source.getFeatures():
            try:
                all_sub_ids.add(int(sub_feat['id']))
            except (ValueError, TypeError):
                pass

        # claimed: polygon ids that have exactly one centroid so far
        claimed = {sid for sid, cents in poly_to_cents.items() if len(cents) >= 1}
        overflow = []   # centroid features that need reassignment
        for sid, cents in poly_to_cents.items():
            if len(cents) > 1:
                overflow.extend(cents[1:])  # keep first, overflow the rest

        reassigned = []  # [(cent_fid, old_letter, new_letter)]
        for cent_feat in overflow:
            point = cent_feat.geometry().asPoint()
            unclaimed = [sid for sid in all_sub_ids if sid not in {int(c) for c in claimed}]
            if not unclaimed:
                feedback.pushWarning(
                    f'Centroid fid {cent_feat.id()} is a duplicate and no unclaimed polygon '
                    f'was found to reassign it to. It will have no name.'
                )
                cent_to_poly[cent_feat.id()] = None
                continue
            # Find nearest unclaimed polygon by distance to its centroid
            nearest_sid = min(
                unclaimed,
                key=lambda sid: point.distance(sub_centroids[sid])
            )
            old_letter = id_to_letter(cent_to_poly[cent_feat.id()])
            new_letter = id_to_letter(nearest_sid)
            cent_to_poly[cent_feat.id()] = nearest_sid
            claimed.add(nearest_sid)
            reassigned.append((cent_feat.id(), old_letter, new_letter))

        if multiple_centroids:
            feedback.pushWarning(
                f'{len(multiple_centroids)} centroid(s) fell inside more than one '
                f'subcatchment polygon. Only the first match was used.'
            )

        if reassigned:
            for cent_fid, old_l, new_l in reassigned:
                feedback.pushWarning(
                    f'Centroid fid {cent_fid} was inside polygon "{old_l}" (already taken) — '
                    f'auto-reassigned to nearest unclaimed polygon "{new_l}". '
                    f'Check that this centroid is in the correct subcatchment.'
                )

        # --- Pass 3: write output ---
        fi_field_exists = 'fi' in [f.name() for f in in_fields]
        for i, cent_feat in enumerate(cent_features):
            matched_sub_id = cent_to_poly[cent_feat.id()]
            letter_id = id_to_letter(matched_sub_id) if matched_sub_id is not None else None

            fi_val = 0.0
            if fi_field_exists:
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
            feedback.setProgress(int((i + 1) / len(cent_features) * 100) if cent_features else 0)

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
            "If a centroid falls inside a polygon that already has a centroid, it is "
            "automatically reassigned to the nearest unclaimed polygon, and a warning "
            "is shown. Check the warning and verify the assignment is correct.\n\n"
            "Also ensures a 'fi' (fraction impervious) field exists, defaulting to 0.0."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return AutoNameCentroidsAlgorithm()
