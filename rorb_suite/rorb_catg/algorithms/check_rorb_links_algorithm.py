# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

from collections import defaultdict

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsFeature,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsProject,
    QgsGeometry,
    QgsPointXY,
)
from ..compat import TYPE_POINT, TYPE_LINE


class CheckRorbLinksAlgorithm(QgsProcessingAlgorithm):
    """Check that reach IDs match their connected node IDs, and report connectivity."""

    IN_CENTROIDS = 'IN_CENTROIDS'
    IN_CONFLUENCES = 'IN_CONFLUENCES'
    IN_REACHES = 'IN_REACHES'
    TOLERANCE = 'TOLERANCE'

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
                self.tr('Reaches layer (with id field)'),
                [TYPE_LINE]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOLERANCE,
                self.tr('Snap tolerance (map units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0001,
                minValue=0.0
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        cent_source = self.parameterAsSource(parameters, self.IN_CENTROIDS, context)
        conf_source = self.parameterAsSource(parameters, self.IN_CONFLUENCES, context)
        reach_source = self.parameterAsSource(parameters, self.IN_REACHES, context)
        tolerance = self.parameterAsDouble(parameters, self.TOLERANCE, context)

        reach_crs = reach_source.sourceCrs()

        # Load all nodes into a combined list, transforming to reach CRS
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
                nodes.append((str(feat['id']), geom, feat))

        load_nodes(cent_source)
        load_nodes(conf_source)

        # Build spatial index
        nodes_index = QgsSpatialIndex()
        for i, (node_id, geom, _) in enumerate(nodes):
            f = QgsFeature()
            f.setId(i)
            f.setGeometry(geom)
            nodes_index.insertFeature(f)

        def find_nearest_point(point_geom):
            candidate_ids = nodes_index.intersects(point_geom.boundingBox())
            best = None
            best_dist = float('inf')
            for fid in candidate_ids:
                node_id, node_geom, node_feat = nodes[fid]
                dist = node_geom.distance(point_geom)
                if dist <= tolerance and dist < best_dist:
                    best_dist = dist
                    best = (node_id, node_feat)
            return best

        discrepancies = []
        point_line_map = defaultdict(list)  # node_id → list of reach ids

        for reach_feat in reach_source.getFeatures():
            geom = reach_feat.geometry()
            line_id = str(reach_feat['id']) if reach_feat['id'] else ''

            polyline = None
            if not geom.isEmpty():
                if geom.isMultipart():
                    parts = geom.asMultiPolyline()
                    if parts:
                        polyline = parts[0]
                else:
                    polyline = geom.asPolyline()

            if not polyline or len(polyline) < 2:
                feedback.pushWarning(f'Could not get endpoints for reach "{line_id}"')
                continue

            start_geom = QgsGeometry.fromPointXY(QgsPointXY(polyline[0]))
            end_geom = QgsGeometry.fromPointXY(QgsPointXY(polyline[-1]))

            start_match = find_nearest_point(start_geom)
            end_match = find_nearest_point(end_geom)

            if start_match is None or end_match is None:
                feedback.pushInfo(f'Could not find matching node for reach "{line_id}"')
                continue

            start_id = start_match[0]
            end_id = end_match[0]
            expected_id = f'{start_id}_{end_id}'

            if line_id != expected_id:
                discrepancies.append((line_id, expected_id))
                feedback.pushInfo(
                    f'ID mismatch — reach "{line_id}": found "{line_id}", expected "{expected_id}"'
                )

            point_line_map[start_id].append(line_id)
            point_line_map[end_id].append(line_id)

        if discrepancies:
            feedback.pushInfo(f'\n{len(discrepancies)} discrepancy(ies) found:')
            for found, expected in discrepancies:
                feedback.pushInfo(f'  Reach "{found}" → expected "{expected}"')
        else:
            feedback.pushInfo('No discrepancies found. All reach id attributes are correct.')

        # Connectivity check: every node should connect to at least one reach
        feedback.pushInfo('\nChecking point-to-line connectivity...')

        cent_count = cent_source.featureCount()
        conf_count = conf_source.featureCount()
        node_idx = 0

        feedback.pushInfo('\nCentroid nodes:')
        for i, (node_id, geom, _) in enumerate(nodes[:cent_count]):
            connected = point_line_map[node_id]
            if connected:
                feedback.pushInfo(
                    f'  Node {node_id}: {len(connected)} line(s) — {connected}'
                )
            else:
                feedback.pushWarning(f'  Node {node_id}: NO connected lines [FLAG]')

        feedback.pushInfo('\nConfluence nodes:')
        for i, (node_id, geom, _) in enumerate(nodes[cent_count:]):
            connected = point_line_map[node_id]
            if connected:
                feedback.pushInfo(
                    f'  Node {node_id}: {len(connected)} line(s) — {connected}'
                )
            else:
                feedback.pushWarning(f'  Node {node_id}: NO connected lines [FLAG]')

        return {}

    def name(self):
        return 'check_rorb_links'

    def displayName(self):
        return self.tr('Check RORB Links')

    def group(self):
        return self.tr(self.groupId())

    def groupId(self):
        return 'Check / Clean RORB Layers'

    def shortHelpString(self):
        return self.tr(
            "Check that reach line IDs are consistent with their connected nodes, "
            "and that every node connects to at least one reach.\n\n"
            "For each reach, the expected id is 'startNodeId_endNodeId'. Any mismatch "
            "is reported in the log. Nodes with no connected reaches are flagged.\n\n"
            "Results are shown in the Processing log — no output layer is created."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CheckRorbLinksAlgorithm()
