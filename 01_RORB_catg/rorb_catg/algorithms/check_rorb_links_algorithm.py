# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

import os
from collections import defaultdict

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorDestination,
    QgsProcessingUtils,
    QgsFeature,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsProject,
    QgsGeometry,
    QgsPointXY,
)
from ..compat import FAST_INSERT, TYPE_POINT, TYPE_LINE

_STYLES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'styles')


class CheckRorbLinksAlgorithm(QgsProcessingAlgorithm):
    """Check that reach IDs match their connected node IDs, and report connectivity."""

    IN_CENTROIDS   = 'IN_CENTROIDS'
    IN_CONFLUENCES = 'IN_CONFLUENCES'
    IN_REACHES     = 'IN_REACHES'
    TOLERANCE      = 'TOLERANCE'
    OUTPUT         = 'OUTPUT'

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
        self.addParameter(
            QgsProcessingParameterVectorDestination(
                self.OUTPUT,
                self.tr('Flagged confluences (out=99 for unconnected nodes)')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        cent_source  = self.parameterAsSource(parameters, self.IN_CENTROIDS,   context)
        conf_source  = self.parameterAsSource(parameters, self.IN_CONFLUENCES, context)
        reach_source = self.parameterAsSource(parameters, self.IN_REACHES,     context)
        tolerance    = self.parameterAsDouble(parameters, self.TOLERANCE,      context)

        reach_crs = reach_source.sourceCrs()

        # Load all nodes (centroids first, then confluences) transformed to reach CRS
        nodes = []  # (id_str, geom, feat, is_confluence)

        def load_nodes(source, is_conf):
            xform = None
            if source.sourceCrs() != reach_crs:
                xform = QgsCoordinateTransform(
                    source.sourceCrs(), reach_crs, QgsProject.instance())
            for feat in source.getFeatures():
                geom = feat.geometry()
                if xform:
                    geom.transform(xform)
                nodes.append((str(feat['id']), geom, feat, is_conf))

        load_nodes(cent_source, False)
        cent_count = len(nodes)
        load_nodes(conf_source, True)

        # Spatial index over all nodes
        nodes_index = QgsSpatialIndex()
        for i, (_, geom, _, _) in enumerate(nodes):
            f = QgsFeature(); f.setId(i); f.setGeometry(geom)
            nodes_index.insertFeature(f)

        def nearest(pt_geom):
            best, best_d = None, float('inf')
            for fid in nodes_index.intersects(pt_geom.boundingBox()):
                d = nodes[fid][1].distance(pt_geom)
                if d <= tolerance and d < best_d:
                    best_d, best = d, nodes[fid]
            return best

        # Check reaches
        discrepancies = []
        point_line_map = defaultdict(list)   # node_id → reach ids connected

        for feat in reach_source.getFeatures():
            geom   = feat.geometry()
            rid    = str(feat['id']) if feat['id'] else ''
            poly   = (geom.asMultiPolyline()[0] if geom.isMultipart()
                      else geom.asPolyline()) if not geom.isEmpty() else None

            if not poly or len(poly) < 2:
                feedback.pushWarning(f'Could not get endpoints for reach "{rid}"')
                continue

            s_match = nearest(QgsGeometry.fromPointXY(QgsPointXY(poly[0])))
            e_match = nearest(QgsGeometry.fromPointXY(QgsPointXY(poly[-1])))

            if s_match is None or e_match is None:
                feedback.pushInfo(f'Could not find matching node for reach "{rid}"')
                continue

            expected = f'{s_match[0]}_{e_match[0]}'
            if rid != expected:
                discrepancies.append((rid, expected))
                feedback.pushInfo(
                    f'ID mismatch — reach "{rid}": found "{rid}", expected "{expected}"'
                )

            point_line_map[s_match[0]].append(rid)
            point_line_map[e_match[0]].append(rid)

        if discrepancies:
            feedback.pushInfo(f'\n{len(discrepancies)} discrepancy(ies) found:')
            for found, expected in discrepancies:
                feedback.pushInfo(f'  Reach "{found}" → expected "{expected}"')
        else:
            feedback.pushInfo('No discrepancies found. All reach id attributes are correct.')

        # Connectivity report
        feedback.pushInfo('\nCentroid nodes:')
        for node_id, _, _, _ in nodes[:cent_count]:
            connected = point_line_map[node_id]
            if connected:
                feedback.pushInfo(f'  Node {node_id}: {len(connected)} line(s) — {connected}')
            else:
                feedback.pushWarning(f'  Node {node_id}: NO connected lines [FLAG]')

        feedback.pushInfo('\nConfluence nodes:')
        invalid_conf_ids = set()
        for node_id, _, _, _ in nodes[cent_count:]:
            connected = point_line_map[node_id]
            if connected:
                feedback.pushInfo(f'  Node {node_id}: {len(connected)} line(s) — {connected}')
            else:
                feedback.pushWarning(f'  Node {node_id}: NO connected lines [FLAG]')
                invalid_conf_ids.add(node_id)

        # Output flagged confluence layer (out=99 for unconnected nodes)
        conf_fields = conf_source.fields()
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            conf_fields, conf_source.wkbType(), conf_source.sourceCrs()
        )
        self._dest_id = dest_id

        out_idx = conf_fields.indexFromName('out')
        total   = conf_source.featureCount()
        for i, feat in enumerate(conf_source.getFeatures()):
            out_feat = QgsFeature(feat)
            node_id  = str(feat['id'])
            if node_id in invalid_conf_ids and out_idx >= 0:
                attrs = out_feat.attributes()
                attrs[out_idx] = 99
                out_feat.setAttributes(attrs)
            sink.addFeature(out_feat, FAST_INSERT)
            feedback.setProgress(int((i + 1) / total * 100) if total else 0)

        return {self.OUTPUT: dest_id}

    def postProcessAlgorithm(self, context, feedback):
        layer = QgsProcessingUtils.mapLayerFromString(self._dest_id, context)
        if layer:
            qml = os.path.join(_STYLES_DIR, 'confluence_check.qml')
            if os.path.isfile(qml):
                layer.loadNamedStyle(qml)
                layer.triggerRepaint()
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
            "For each reach the expected id is 'startNodeId_endNodeId'. Any mismatch "
            "is reported in the log. Nodes with no connected reaches are flagged.\n\n"
            "Outputs a copy of the confluences layer with out=99 on any unconnected "
            "confluence nodes, styled with the check symbology so problem nodes are "
            "immediately visible."
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CheckRorbLinksAlgorithm()
