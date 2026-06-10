import math
from pyromb.core.gis.vector_layer import VectorLayer


class QVectorLayer(VectorLayer):
    def __init__(self, feature_source) -> None:
        if isinstance(feature_source, list):
            self._features = feature_source
        else:
            self._features = [f for f in feature_source.getFeatures()]

    def geometry(self, i) -> list:
        """returns the geometry of the ith vector in the shapefile"""
        return [(p.x(), p.y()) for p in self._features[i].geometry().vertices()]

    def record(self, i) -> dict:
        """returns the set of attributes for the ith vector in the shapefile"""
        return self._features[i]

    def __len__(self) -> int:
        """return the number of vectors in the shapefile"""
        return self._features.__len__()


class SnappedQVectorLayer(QVectorLayer):
    """QVectorLayer whose reach endpoint geometries have been snapped to nodes."""

    def __init__(self, features, snapped_geometries) -> None:
        super().__init__(features)
        self._snapped_geometries = snapped_geometries

    def geometry(self, i) -> list:
        return self._snapped_geometries[i]


def snap_reach_endpoints(reach_features, node_feature_lists, tolerance):
    """
    Snap reach line start/end points to the nearest node within tolerance.

    reach_features       -- list of QgsFeature for reach lines
    node_feature_lists   -- list of feature lists to snap to (e.g. confluences + centroids)
    tolerance            -- max snap distance in map units (0 disables)

    Returns a list of coordinate-tuple lists, one per reach, with endpoints
    snapped where a node was found within tolerance.
    """
    nodes = []
    for feature_list in node_feature_lists:
        for f in feature_list:
            pt = f.geometry().asPoint()
            nodes.append((pt.x(), pt.y()))

    result = []
    for feature in reach_features:
        coords = [(v.x(), v.y()) for v in feature.geometry().vertices()]
        if len(coords) >= 2:
            for idx in (0, len(coords) - 1):
                px, py = coords[idx]
                best_dist = tolerance
                best_node = None
                for nx, ny in nodes:
                    d = math.sqrt((nx - px) ** 2 + (ny - py) ** 2)
                    if d < best_dist:
                        best_dist = d
                        best_node = (nx, ny)
                if best_node is not None:
                    coords[idx] = best_node
        result.append(coords)
    return result
