# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorDestination,
    QgsProcessingParameterNumber,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsProject,
    QgsGeometry,
    QgsPointXY,
)
from ..compat import (INT, DOUBLE, STRING, FAST_INSERT,
                      TYPE_POINT, TYPE_LINE,
                      wkb_geometry_type, WKB_LINE_GEOMETRY)


class AutoNameReachesAlgorithm(QgsProcessingAlgorithm):
    """Name reach lines using the IDs of their nearest start and end nodes."""

    IN_CENTROIDS = 'IN_CENTROIDS'
    IN_CONFLUENCES = 'IN_CONFLUENCES'
    IN_REACHES = 'IN_REACHES'
    SEARCH_RADIUS = 'SEARCH_RADIUS'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CENTROIDS,
                self.tr('Centroids layer (with id field)'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_CONFLUENCES,
                self.tr('Confluences layer (with id field)'),
                [TYPE_POINT]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.IN_REACHES,
                self.tr('Reaches layer'),
                [TYPE_LINE]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SEARCH_RADIUS,
                self.tr('Node search radius (map units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=50.0,
                minValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Named reaches')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        cent_source = self.parameterAsSource(parameters, self.IN_CENTROIDS, context)
        conf_source = self.parameterAsSource(parameters, self.IN_CONFLUENCES, context)
        reach_source = self.parameterAsSource(parameters, self.IN_REACHES, context)
        search_radius = self.parameterAsDouble(parameters, self.SEARCH_RADIUS, context)

        reach_crs = reach_source.sourceCrs()

        # Build output fields: ensure 't' (int), 's' (float), 'id' (str)
        in_fields = reach_source.fields()
        out_fields = QgsFields()
        reserved = {'id', 't', 's'}
        for field in in_fields:
            if field.name() not in reserved:
                out_fields.append(field)
        out_fields.append(QgsField('t', INT))
        out_fields.append(QgsField('s', DOUBLE))
        out_fields.append(QgsField('id', STRING))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, reach_source.wkbType(), reach_crs
        )

        # Load all nodes (centroids + confluences), transforming to reach CRS
        nodes = []  # list of (id_str, QgsGeometry)

        def load_nodes(node_source):
            node_crs = node_source.sourceCrs()
            transform = None
            if node_crs != reach_crs:
                transform = QgsCoordinateTransform(node_crs, reach_crs, QgsProject.instance())
            for feat in node_source.getFeatures():
                geom = feat.geometry()
                if transform:
                    geom.transform(transform)
                nodes.append((str(feat['id']), geom))

        load_nodes(cent_source)
        load_nodes(conf_source)

        # Build spatial index over nodes
        nodes_index = QgsSpatialIndex()
        for i, (node_id, geom) in enumerate(nodes):
            f = QgsFeature()
            f.setId(i)
            f.setGeometry(geom)
            nodes_index.insertFeature(f)

        def find_nearest_node(point_geom):
            search_area = point_geom.buffer(search_radius, 5)
            candidate_ids = nodes_index.intersects(search_area.boundingBox())
            best_id = None
            best_dist = float('inf')
            for fid in candidate_ids:
                node_id, node_geom = nodes[fid]
                if node_geom.intersects(search_area):
                    dist = node_geom.distance(point_geom)
                    if dist < best_dist:
                        best_dist = dist
                        best_id = node_id
            return best_id

        total = reach_source.featureCount()
        unnamed = []

        for i, feat in enumerate(reach_source.getFeatures()):
            geom = feat.geometry()
            reach_id = None

            if not geom.isEmpty():
                wkb_type = geom.wkbType()
                if wkb_geometry_type(wkb_type) == WKB_LINE_GEOMETRY:
                    # Get actual polyline coords regardless of Multi/Single
                    polyline = None
                    if geom.isMultipart():
                        parts = geom.asMultiPolyline()
                        if parts:
                            polyline = parts[0]
                    else:
                        polyline = geom.asPolyline()

                    if polyline and len(polyline) >= 2:
                        start_geom = QgsGeometry.fromPointXY(QgsPointXY(polyline[0]))
                        end_geom = QgsGeometry.fromPointXY(QgsPointXY(polyline[-1]))
                        from_id = find_nearest_node(start_geom)
                        to_id = find_nearest_node(end_geom)
                        if from_id and to_id:
                            reach_id = f"{from_id}_{to_id}"

            if reach_id is None:
                unnamed.append(i)

            # Get t and s from existing attributes, default to 1 and 0.0
            t_val = 1
            if 't' in [f.name() for f in in_fields]:
                try:
                    t_val = int(feat['t']) if feat['t'] is not None else 1
                except (ValueError, TypeError):
                    t_val = 1

            s_val = 0.0
            if 's' in [f.name() for f in in_fields]:
                try:
                    s_val = float(feat['s']) if feat['s'] is not None else 0.0
                except (ValueError, TypeError):
                    s_val = 0.0

            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(feat.geometry())
            attrs = []
            for field in in_fields:
                if field.name() not in reserved:
                    attrs.append(feat[field.name()])
            attrs.append(t_val)
            attrs.append(s_val)
            attrs.append(reach_id if reach_id else '')
            new_feat.setAttributes(attrs)
            sink.addFeature(new_feat, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100) if total else 0)

        if unnamed:
            feedback.pushWarning(
                f'{len(unnamed)} reach(es) could not be named — no node found within '
                f'{search_radius} map units of their endpoints. Their id was set to empty string.'
            )

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'auto_name_reaches'

    def displayName(self):
        return self.tr('Auto Name Reaches')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Prepare RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Name reach lines by finding the nearest node at each endpoint.\n\n"
            "Searches for the closest centroid or confluence point within the search "
            "radius of each reach's start and end vertices. Assigns id = 'fromNode_toNode'.\n\n"
            "Also ensures 't' (reach type, integer) and 's' (slope, float) fields exist, "
            "defaulting to 1 and 0.0 respectively.\n\n"
            "Run Auto Name Centroids and Auto Name Confluences before this step."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return AutoNameReachesAlgorithm()
