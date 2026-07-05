# -*- coding: utf-8 -*-
"""
Core pipeline functions: name layers and save as shapefiles.
Each function takes raw QgsVectorLayer(s) and an output path,
writes a new shapefile, and returns the loaded QgsVectorLayer.
"""

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

import os
import shutil
import string
import time
from collections import defaultdict

from qgis.core import (
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsSpatialIndex,
    QgsGeometry,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
)
from .compat import INT, DOUBLE, STRING, make_shapefile_writer


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _id_to_uppercase(subcatchment_id):
    """1→A, 2→B, …, 26→Z, 27→AA, …"""
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


def _generate_lowercase_ids(n):
    """a, b, …, z, aa, ab, …"""
    letters = string.ascii_lowercase
    base = len(letters)
    ids = []
    for i in range(n):
        k, result = i, ''
        while True:
            result = letters[k % base] + result
            k = k // base - 1
            if k < 0:
                break
        ids.append(result)
    return ids


def _build_output_fields(in_fields, replacements):
    """
    Build QgsFields from in_fields, dropping any field whose name is in
    replacements, then appending the replacement fields in order.
    """
    out = QgsFields()
    drop = {name for name, _ in replacements}
    for f in in_fields:
        if f.name() not in drop:
            out.append(f)
    for name, qtype in replacements:
        out.append(QgsField(name, qtype))
    return out


SHAPEFILE_EXTS = ('.shp', '.dbf', '.shx', '.prj', '.cpg', '.qpj', '.idx', '.sbn', '.sbx')


def layer_file_path(layer):
    """Return the on-disk path of a vector layer's source file (no OGR sublayer suffix)."""
    return layer.source().split('|')[0]


def is_rewritable_shapefile(layer):
    """True if layer is an OGR-backed .shp file that exists on disk."""
    path = layer_file_path(layer)
    return (layer.dataProvider() is not None
            and layer.providerType() == 'ogr'
            and path.lower().endswith('.shp')
            and os.path.isfile(path))


def replace_shapefile(tmp_path, final_path, retries=20, delay=0.15):
    """
    Copy the shapefile (and sidecars) at tmp_path over final_path, replacing
    it in place. Retries briefly since Windows may hold the original file
    handle open for a moment after its QgsVectorLayer is removed from the
    project. Copies (rather than moves/renames) so an open read handle on
    tmp_path doesn't block the operation.
    """
    import gc
    from qgis.PyQt.QtWidgets import QApplication

    tmp_base = os.path.splitext(tmp_path)[0]
    last_err = None
    for attempt in range(retries):
        # QgsProject.removeMapLayer() schedules the layer's C++ object for
        # deletion via deleteLater() — pump the event loop and force garbage
        # collection so the OGR file handle it holds is actually released
        # before we try to overwrite it.
        gc.collect()
        QApplication.processEvents()
        try:
            _delete_shapefile_if_exists(final_path)
            break
        except OSError as e:
            last_err = e
            time.sleep(delay)
    else:
        raise RuntimeError(
            f'Could not overwrite "{final_path}" — the file appears to be '
            f'locked (still open elsewhere): {last_err}')

    final_base = os.path.splitext(final_path)[0]
    for ext in SHAPEFILE_EXTS:
        src = tmp_base + ext
        if os.path.exists(src):
            shutil.copy2(src, final_base + ext)


def _delete_shapefile_if_exists(path):
    """Remove all sidecar files for an existing shapefile."""
    base = os.path.splitext(path)[0]
    for ext in SHAPEFILE_EXTS:
        candidate = base + ext
        if os.path.exists(candidate):
            os.remove(candidate)


def _write_shapefile(features, fields, wkb_type, crs, output_path):
    """Overwrite any existing shapefile and return the loaded layer."""
    _delete_shapefile_if_exists(output_path)
    writer = make_shapefile_writer(output_path, fields, wkb_type, crs)
    if writer.hasError() != QgsVectorFileWriter.NoError:
        raise RuntimeError(f'Could not create shapefile: {writer.errorMessage()}')
    for feat in features:
        writer.addFeature(feat)
    del writer  # flush & close

    layer = QgsVectorLayer(output_path, os.path.splitext(os.path.basename(output_path))[0], 'ogr')
    if not layer.isValid():
        raise RuntimeError(f'Saved shapefile is not a valid layer: {output_path}')
    return layer


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Name subcatchments south → north
# ─────────────────────────────────────────────────────────────────────────────

def name_subcatchments(source_layer, output_path):
    """
    Number polygon subcatchments 1, 2, 3, … from south to north.
    Saves to output_path (.shp). Returns the loaded QgsVectorLayer.
    """
    in_fields = source_layer.fields()
    out_fields = _build_output_fields(in_fields, [('id', INT)])
    drop = {'id'}

    crs = source_layer.crs()
    # Project to get accurate centroid y even for geographic CRS
    if crs.isGeographic():
        proj_crs = QgsCoordinateReferenceSystem('EPSG:3857')
        transform = QgsCoordinateTransform(crs, proj_crs, QgsProject.instance())
    else:
        transform = None

    rows = []
    feats = source_layer.getFeatures()
    for feat in feats:
        geom = feat.geometry()
        if transform:
            g2 = QgsGeometry(geom)
            g2.transform(transform)
            y = g2.centroid().asPoint().y()
        else:
            y = geom.centroid().asPoint().y()
        rows.append((y, feat))
    feats.close()

    rows.sort(key=lambda r: r[0])  # ascending y = south → north

    out_feats = []
    for i, (_, feat) in enumerate(rows):
        f = QgsFeature(out_fields)
        f.setGeometry(feat.geometry())
        attrs = [feat[fld.name()] for fld in in_fields if fld.name() not in drop]
        attrs.append(i + 1)
        f.setAttributes(attrs)
        out_feats.append(f)

    return _write_shapefile(out_feats, out_fields, source_layer.wkbType(), crs, output_path)


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Name centroids by spatial join to named subcatchments
# ─────────────────────────────────────────────────────────────────────────────

def name_centroids(named_subs_layer, cent_layer, output_path):
    """
    Assign letter IDs (A, B, …) to centroid points via a spatial join to the
    numbered subcatchment polygons. Adds/updates 'id' (str) and 'fi' (float).
    Saves to output_path (.shp). Returns the loaded QgsVectorLayer.
    """
    in_fields = cent_layer.fields()
    out_fields = _build_output_fields(
        in_fields, [('id', STRING), ('fi', DOUBLE)]
    )
    drop = {'id', 'fi'}

    cent_crs = cent_layer.crs()
    sub_crs  = named_subs_layer.crs()
    sub_transform = (QgsCoordinateTransform(sub_crs, cent_crs, QgsProject.instance())
                     if sub_crs != cent_crs else None)

    # Build spatial index over subcatchments (in centroid CRS)
    sub_index = QgsSpatialIndex()
    sub_dict  = {}
    feats = named_subs_layer.getFeatures()
    for sub_feat in feats:
        geom = sub_feat.geometry()
        if sub_transform:
            geom.transform(sub_transform)
        f = QgsFeature(sub_feat.id())
        f.setGeometry(geom)
        sub_index.insertFeature(f)
        sub_dict[sub_feat.id()] = (geom, sub_feat['id'])  # (geom, numeric_id)
    feats.close()

    out_feats = []
    feats = cent_layer.getFeatures()
    for cent_feat in feats:
        pt = cent_feat.geometry()
        candidates = sub_index.intersects(pt.boundingBox())
        matched_numeric_id = None
        for fid in candidates:
            sub_geom, sub_num_id = sub_dict[fid]
            if sub_geom.contains(pt):
                matched_numeric_id = sub_num_id
                break

        letter = _id_to_uppercase(matched_numeric_id) if matched_numeric_id is not None else ''

        fi_val = 0.0
        if 'fi' in [fld.name() for fld in in_fields]:
            try:
                fi_val = float(cent_feat['fi']) if cent_feat['fi'] is not None else 0.0
            except (ValueError, TypeError):
                fi_val = 0.0

        f = QgsFeature(out_fields)
        f.setGeometry(cent_feat.geometry())
        attrs = [cent_feat[fld.name()] for fld in in_fields if fld.name() not in drop]
        attrs.append(str(letter))
        attrs.append(fi_val)
        f.setAttributes(attrs)
        out_feats.append(f)
    feats.close()

    return _write_shapefile(out_feats, out_fields, cent_layer.wkbType(), cent_crs, output_path)


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Name confluences south → north
# ─────────────────────────────────────────────────────────────────────────────

def name_confluences(source_layer, output_path):
    """
    Assign lowercase letter IDs (a, b, …) to confluence points from south to north.
    Adds/updates 'id' (str) and 'out' (int). Saves to output_path (.shp).
    Returns the loaded QgsVectorLayer.
    """
    in_fields = source_layer.fields()
    out_fields = _build_output_fields(
        in_fields, [('id', STRING), ('out', INT)]
    )
    drop = {'id', 'out'}

    crs = source_layer.crs()
    if crs.isGeographic():
        proj_crs = QgsCoordinateReferenceSystem('EPSG:28351')
        transform = QgsCoordinateTransform(crs, proj_crs, QgsProject.instance())
    else:
        transform = None

    rows = []
    feats = source_layer.getFeatures()
    for feat in feats:
        geom = feat.geometry()
        if transform:
            g2 = QgsGeometry(geom)
            g2.transform(transform)
            y = g2.asPoint().y()
        else:
            y = geom.asPoint().y()
        rows.append((y, feat))
    feats.close()

    rows.sort(key=lambda r: r[0])
    id_list = _generate_lowercase_ids(len(rows))

    out_feats = []
    for i, (_, feat) in enumerate(rows):
        out_val = 0
        if 'out' in [fld.name() for fld in in_fields]:
            try:
                out_val = int(feat['out']) if feat['out'] is not None else 0
            except (ValueError, TypeError):
                out_val = 0

        f = QgsFeature(out_fields)
        f.setGeometry(feat.geometry())
        attrs = [feat[fld.name()] for fld in in_fields if fld.name() not in drop]
        attrs.append(id_list[i])
        attrs.append(out_val)
        f.setAttributes(attrs)
        out_feats.append(f)

    return _write_shapefile(out_feats, out_fields, source_layer.wkbType(), crs, output_path)


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Name reaches by fromNode_toNode
# ─────────────────────────────────────────────────────────────────────────────

SNAP_RADIUS = 50.0  # map units


def name_reaches(named_cents_layer, named_confs_layer, reach_layer, output_path,
                 search_radius=SNAP_RADIUS):
    """
    Assign id = 'fromNode_toNode' to each reach by snapping endpoints to the
    nearest centroid or confluence node. Ensures 't' (int) and 's' (float) fields.
    Saves to output_path (.shp). Returns the loaded QgsVectorLayer.
    """
    in_fields = reach_layer.fields()
    out_fields = _build_output_fields(
        in_fields,
        [('t', INT), ('s', DOUBLE), ('id', STRING)]
    )
    drop = {'t', 's', 'id'}

    reach_crs = reach_layer.crs()

    # Load all nodes in reach CRS
    nodes = []  # [(id_str, QgsGeometry)]

    def _load(layer):
        t = (QgsCoordinateTransform(layer.crs(), reach_crs, QgsProject.instance())
             if layer.crs() != reach_crs else None)
        feats = layer.getFeatures()
        for feat in feats:
            geom = feat.geometry()
            if t:
                geom.transform(t)
            nodes.append((str(feat['id']), geom))
        feats.close()

    _load(named_cents_layer)
    _load(named_confs_layer)

    node_idx = QgsSpatialIndex()
    for i, (nid, geom) in enumerate(nodes):
        f = QgsFeature()
        f.setId(i)
        f.setGeometry(geom)
        node_idx.insertFeature(f)

    def _nearest(pt_geom):
        buf = pt_geom.buffer(search_radius, 5)
        best_id, best_dist = None, float('inf')
        for fid in node_idx.intersects(buf.boundingBox()):
            nid, ngeom = nodes[fid]
            if ngeom.intersects(buf):
                d = ngeom.distance(pt_geom)
                if d < best_dist:
                    best_dist, best_id = d, nid
        return best_id

    out_feats = []
    unnamed = []

    feats = reach_layer.getFeatures()
    for feat in feats:
        geom = feat.geometry()
        reach_id = ''

        if not geom.isEmpty():
            polyline = (geom.asMultiPolyline()[0] if geom.isMultipart()
                        else geom.asPolyline())
            if polyline and len(polyline) >= 2:
                from_id = _nearest(QgsGeometry.fromPointXY(QgsPointXY(polyline[0])))
                to_id   = _nearest(QgsGeometry.fromPointXY(QgsPointXY(polyline[-1])))
                if from_id and to_id:
                    reach_id = f'{from_id}_{to_id}'
                else:
                    unnamed.append(feat.id())

        t_val = 1
        if 't' in [fld.name() for fld in in_fields]:
            try:
                t_val = int(feat['t']) if feat['t'] is not None else 1
            except (ValueError, TypeError):
                t_val = 1

        s_val = 0.0
        if 's' in [fld.name() for fld in in_fields]:
            try:
                s_val = float(feat['s']) if feat['s'] is not None else 0.0
            except (ValueError, TypeError):
                s_val = 0.0

        f = QgsFeature(out_fields)
        f.setGeometry(feat.geometry())
        attrs = [feat[fld.name()] for fld in in_fields if fld.name() not in drop]
        attrs += [t_val, s_val, reach_id]
        f.setAttributes(attrs)
        out_feats.append(f)
    feats.close()

    result_layer = _write_shapefile(out_feats, out_fields, reach_layer.wkbType(),
                                    reach_crs, output_path)
    return result_layer, unnamed


# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Check link topology (returns structured results)
# ─────────────────────────────────────────────────────────────────────────────

CHECK_SNAP = 0.5  # map units


def run_checks(reach_layer, cent_layer, conf_layer):
    """
    Returns (results, error_reach_ids, error_node_ids).
    results          – list of (status, message) where status ∈ {'pass', 'fail', 'warn'}
    error_reach_ids  – set of reach 'id' strings with any error/warning
    error_node_ids   – set of node 'id' strings with any error/warning
    """
    results = []
    error_reach_ids = set()
    error_node_ids  = set()

    def _check_fields(layer, required, label):
        names = [f.name() for f in layer.fields()]
        missing = [r for r in required if r not in names]
        if missing:
            results.append(('fail', f"{label}: missing field(s): {', '.join(missing)}"))
            return False
        results.append(('pass', f"{label}: required fields present ({', '.join(required)})"))
        return True

    ok = all([
        _check_fields(cent_layer,  ['id'],            'Centroids'),
        _check_fields(conf_layer,  ['id'],            'Confluences'),
        _check_fields(reach_layer, ['id', 't', 's'], 'Reaches'),
    ])
    if not ok:
        results.append(('warn', 'Skipping topology checks — fix missing fields first.'))
        return results, error_reach_ids, error_node_ids

    reach_crs = reach_layer.crs()
    nodes = []

    def _load(layer):
        t = (QgsCoordinateTransform(layer.crs(), reach_crs, QgsProject.instance())
             if layer.crs() != reach_crs else None)
        feats = layer.getFeatures()
        for feat in feats:
            geom = feat.geometry()
            if t:
                geom.transform(t)
            nodes.append((str(feat['id']), geom))
        feats.close()

    _load(cent_layer)
    n_cents = len(nodes)
    _load(conf_layer)

    idx = QgsSpatialIndex()
    for i, (_, geom) in enumerate(nodes):
        f = QgsFeature()
        f.setId(i)
        f.setGeometry(geom)
        idx.insertFeature(f)

    def _nearest(pt_geom):
        buf = pt_geom.buffer(CHECK_SNAP, 5)
        best_id, best_dist = None, float('inf')
        for fid in idx.intersects(buf.boundingBox()):
            nid, ngeom = nodes[fid]
            if ngeom.intersects(buf):
                d = ngeom.distance(pt_geom)
                if d < best_dist:
                    best_dist, best_id = d, nid
        return best_id

    mismatches = []
    unmatched  = []
    pt_line_map = defaultdict(list)
    _from_ids = set()
    _to_ids = set()

    for feat in reach_layer.getFeatures():
        geom   = feat.geometry()
        lid    = str(feat['id']) if feat['id'] else ''
        poly   = (geom.asMultiPolyline()[0] if geom.isMultipart() else geom.asPolyline()
                  ) if not geom.isEmpty() else None

        if not poly or len(poly) < 2:
            unmatched.append(lid or '(no id)')
            continue

        from_id = _nearest(QgsGeometry.fromPointXY(QgsPointXY(poly[0])))
        to_id   = _nearest(QgsGeometry.fromPointXY(QgsPointXY(poly[-1])))

        if from_id is None or to_id is None:
            unmatched.append(lid or '(no id)')
            continue

        expected = f'{from_id}_{to_id}'
        pt_line_map[from_id].append(lid)
        pt_line_map[to_id].append(lid)
        _from_ids.add(from_id)
        _to_ids.add(to_id)

        if lid != expected:
            mismatches.append((lid, expected))

    if mismatches:
        for found, expected in mismatches:
            results.append(('fail', f'Reach "{found}": expected "{expected}"'))
            error_reach_ids.add(found)
    else:
        results.append(('pass', 'All reach IDs match their connected nodes'))

    if unmatched:
        results.append(('warn',
            f'{len(unmatched)} reach(es) had no matching node endpoint: {unmatched}'))
        error_reach_ids.update(unmatched)

    isolated = [nid for nid, _ in nodes if not pt_line_map[nid]]
    if isolated:
        for nid in isolated:
            results.append(('fail', f'Node "{nid}": not connected to any reach'))
        error_node_ids.update(isolated)
    else:
        results.append(('pass', f'All {len(nodes)} node(s) connect to at least one reach'))

    # ── Directed connectivity: every confluence must have at least one incoming reach
    # A confluence node with only outgoing reach(es) is a topological dead-end —
    # pyromb cannot determine the calculation order for nodes downstream of it.
    no_incoming = [
        nid for nid, _ in nodes[n_cents:]   # confluences only
        if nid not in _to_ids
    ]
    if no_incoming:
        for nid in no_incoming:
            results.append(('fail',
                f'Confluence "{nid}": has outgoing reach but no incoming reach '
                f'— remove or reconnect this node'))
        error_node_ids.update(no_incoming)
    else:
        results.append(('pass', 'All confluence nodes have at least one incoming reach'))

    neg = []
    for feat in reach_layer.getFeatures():
        try:
            if feat['s'] is not None and float(feat['s']) < 0:
                neg.append(str(feat['id']))
        except (ValueError, TypeError):
            pass

    if neg:
        results.append(('warn', f'{len(neg)} reach(es) have negative slope: {neg}'))
        error_reach_ids.update(neg)
    else:
        results.append(('pass', 'No negative slope values found'))

    # ── Outlet check ─────────────────────────────────────────────────────────
    outlets = _to_ids - _from_ids
    if len(outlets) == 1:
        results.append(('pass', f'Single outlet node: "{next(iter(outlets))}"'))
    elif len(outlets) == 0:
        results.append(('fail',
            'No outlet found — every node has an outgoing reach '
            '(possible circular or disconnected network)'))
    else:
        results.append(('fail',
            f'{len(outlets)} outlet nodes found — must be exactly 1: '
            f'{sorted(outlets)}'))
        error_node_ids.update(outlets)

    # ── Euler check: junctions + centroids = reaches + 1 ─────────────────────
    n_rch = reach_layer.featureCount()
    n_jun = conf_layer.featureCount()
    n_cen = cent_layer.featureCount()
    lhs, rhs = n_jun + n_cen, n_rch + 1
    if lhs == rhs:
        results.append(('pass',
            f'Euler check: {n_jun} junctions + {n_cen} centroids = {n_rch} reaches + 1'))
    else:
        diff = lhs - rhs
        word = 'extra' if diff > 0 else 'missing'
        results.append(('fail',
            f'Euler check: {n_jun} junctions + {n_cen} centroids = {lhs} '
            f'≠ {n_rch} reaches + 1 = {rhs}  ({abs(diff)} {word} node(s))'))

    return results, error_reach_ids, error_node_ids
