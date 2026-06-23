from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsWkbTypes
from .attributes import Basin, Confluence, Reach, ReachType
from .geometry import Point, dist


def _val(v):
    if isinstance(v, QVariant):
        return None if v.isNull() else v.value()
    return v


def _line_coords(geom):
    wkb = geom.wkbType()
    if QgsWkbTypes.isMultiType(wkb):
        parts = geom.asMultiPolyline()
        return [(p.x(), p.y()) for p in parts[0]] if parts else []
    pts = geom.asPolyline()
    return [(p.x(), p.y()) for p in pts]


def _point_coords(geom):
    if QgsWkbTypes.isMultiType(geom.wkbType()):
        pts = geom.asMultiPoint()
        p = pts[0] if pts else None
    else:
        p = geom.asPoint()
    return (p.x(), p.y()) if p else (0.0, 0.0)


def build_reaches(layer, fld_id, fld_slope, fld_type):
    reaches = []
    for feat in layer.getFeatures():
        coords = _line_coords(feat.geometry())
        if not coords:
            continue
        name = str(_val(feat[fld_id]) or feat.id()) if fld_id else str(feat.id())
        raw_slope = _val(feat[fld_slope]) if fld_slope else None
        slope = float(raw_slope) if raw_slope is not None else 0.0
        raw_type = _val(feat[fld_type]) if fld_type else None
        rtype_val = int(raw_type) if raw_type is not None else 1
        try:
            rtype = ReachType(rtype_val)
        except ValueError:
            rtype = ReachType.NATURAL
        reaches.append(Reach(name, coords, rtype, slope))
    return reaches


def build_confluences(layer, fld_id, fld_out):
    confluences = []
    for feat in layer.getFeatures():
        x, y = _point_coords(feat.geometry())
        name = str(_val(feat[fld_id]) or feat.id()) if fld_id else str(feat.id())
        out_raw = _val(feat[fld_out]) if fld_out else 0
        is_out = bool(int(out_raw)) if out_raw is not None else False
        confluences.append(Confluence(name, x, y, is_out))
    return confluences


def build_basins(centroid_layer, basin_layer, fld_id, fld_fi):
    poly_data = []
    for feat in basin_layer.getFeatures():
        area_m2 = feat.geometry().area()
        cp = feat.geometry().centroid().asPoint()
        poly_data.append({'area_km2': area_m2 / 1e6, 'cx': cp.x(), 'cy': cp.y()})

    basins = []
    for feat in centroid_layer.getFeatures():
        x, y = _point_coords(feat.geometry())
        name = str(_val(feat[fld_id]) or feat.id()) if fld_id else str(feat.id())
        raw_fi = _val(feat[fld_fi]) if fld_fi else None
        fi = float(raw_fi) if raw_fi is not None else 0.0

        best_idx, best_d = 0, 1e18
        cp = Point(x, y)
        for k, pd in enumerate(poly_data):
            d = dist(cp, Point(pd['cx'], pd['cy']))
            if d < best_d:
                best_d = d; best_idx = k

        area = poly_data[best_idx]['area_km2'] if poly_data else 0.0
        basins.append(Basin(name, x, y, area, fi))
    return basins
