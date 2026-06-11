import math
from pyromb.core.gis.vector_layer import VectorLayer


def _to_python(val):
    """Convert a QVariant to a native Python type if needed."""
    try:
        from qgis.PyQt.QtCore import QVariant
        if isinstance(val, QVariant):
            return None if val.isNull() else val.toPyObject()
    except Exception:
        pass
    return val


_FIELD_DEFAULTS = {
    't':  1,    # Natural reach type
    's':  0.0,  # Slope
    'fi': 0.0,  # Fraction impervious
}


class _MappedFeature:
    """Wraps a QgsFeature, remapping attribute names via a field map."""

    def __init__(self, feature, field_map):
        self._feature = feature
        self._field_map = field_map  # {pyromb_name: actual_field_name}

    def __getitem__(self, key):
        actual = self._field_map.get(key, key)
        val = _to_python(self._feature[actual]) if actual else None
        if val is None:
            return _FIELD_DEFAULTS.get(key)
        # Clamp reach type: must be 1-4, anything else defaults to 1 (Natural)
        if key == 't' and val not in (1, 2, 3, 4):
            return 1
        return val


class QVectorLayer(VectorLayer):
    def __init__(self, feature_source, field_map=None) -> None:
        if isinstance(feature_source, list):
            self._features = feature_source
        else:
            self._features = list(feature_source.getFeatures())
        self._field_map = field_map or {}

    def geometry(self, i) -> list:
        return [(p.x(), p.y()) for p in self._features[i].geometry().vertices()]

    def record(self, i):
        if self._field_map:
            return _MappedFeature(self._features[i], self._field_map)
        return self._features[i]

    def __len__(self) -> int:
        return len(self._features)


class SnappedQVectorLayer(QVectorLayer):
    def __init__(self, features, snapped_geometries, field_map=None) -> None:
        super().__init__(features, field_map)
        self._snapped_geometries = snapped_geometries

    def geometry(self, i) -> list:
        return self._snapped_geometries[i]


def snap_reach_endpoints(reach_features, node_feature_lists, tolerance):
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
                best_dist, best_node = tolerance, None
                for nx, ny in nodes:
                    d = math.sqrt((nx - px) ** 2 + (ny - py) ** 2)
                    if d < best_dist:
                        best_dist, best_node = d, (nx, ny)
                if best_node:
                    coords[idx] = best_node
        result.append(coords)
    return result
