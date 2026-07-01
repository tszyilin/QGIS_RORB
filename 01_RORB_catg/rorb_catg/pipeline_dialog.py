# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

import os
import shutil
import tempfile

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QTextEdit, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QCheckBox, QComboBox, QHeaderView, QFrame,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont, QColor

from qgis.gui import QgsMapLayerComboBox, QgsHighlight
from qgis.core import (
    QgsProject, QgsDistanceArea, QgsUnitTypes,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
)
from .compat import (ALIGN_RIGHT, ALIGN_CENTER,
                     LAYER_POINT, LAYER_LINE, LAYER_POLYGON)

from .pipeline_utils import (
    name_subcatchments, name_centroids, name_confluences,
    name_reaches, run_checks,
)
from .custom_types.qvector_layer import QVectorLayer

# Purge any previously cached pyromb from sys.modules so that after a plugin
# update (without a full QGIS restart) we always load the freshly-extracted
# vendor copy rather than the old in-memory module.
import sys as _sys
_vendor_dir = os.path.join(os.path.dirname(__file__), 'vendor')
for _k in [k for k in _sys.modules if k == 'pyromb' or k.startswith('pyromb.')]:
    del _sys.modules[_k]
if _vendor_dir not in _sys.path:
    _sys.path.insert(0, _vendor_dir)
import pyromb


class RorbPipelineDialog(QWidget):

    def __init__(self, iface, parent=None, on_open_results=None, on_catg_built=None):
        super().__init__(parent)
        self.iface = iface
        self._on_open_results = on_open_results
        self._on_catg_built   = on_catg_built   # called with catg_path when embedded in Run RORB tab
        self._named_cents   = None
        self._named_confs   = None
        self._named_reaches = None
        self._named_basins  = None
        self._check_results    = []
        self._error_highlights = []   # QgsHighlight objects — kept alive here
        self._tmp_dir          = None
        self._run_dialog       = None
        self._setup_ui()

    def closeEvent(self, event):
        self._clear_error_highlights()
        self._cleanup_tmp()
        super().closeEvent(event)

    def _cleanup_tmp(self):
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

    # ── UI ───────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle('RORB Pipeline')
        self.setMinimumWidth(580)

        # Outer horizontal layout: steps on the left, print panel on the right
        outer = QHBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(0, 0, 0, 0)

        _left = QWidget()
        root = QVBoxLayout(_left)
        root.setSpacing(6)
        outer.addWidget(_left, 1)

        # ── Step 1: layers ───────────────────────────────────────────────────
        grp1 = QGroupBox('Step 1 — Select Input Layers')
        form1 = QFormLayout(grp1)
        form1.setLabelAlignment(ALIGN_RIGHT)

        self.cmb_sub   = QgsMapLayerComboBox(); self.cmb_sub.setFilters(LAYER_POLYGON)
        self.cmb_cent  = QgsMapLayerComboBox(); self.cmb_cent.setFilters(LAYER_POINT)
        self.cmb_conf  = QgsMapLayerComboBox(); self.cmb_conf.setFilters(LAYER_POINT)
        self.cmb_reach = QgsMapLayerComboBox(); self.cmb_reach.setFilters(LAYER_LINE)

        form1.addRow('Subcatchment (polygon):', self.cmb_sub)
        form1.addRow('Centroid (point):', self.cmb_cent)
        form1.addRow('Confluence (point):', self.cmb_conf)
        form1.addRow('Reach (line):', self.cmb_reach)
        root.addWidget(grp1)

        # ── Step 2: optional output folder ───────────────────────────────────
        grp2 = QGroupBox('Step 2 — Output Folder  (leave empty to save as temporary layers)')
        form2 = QFormLayout(grp2)
        form2.setLabelAlignment(ALIGN_RIGHT)

        folder_row = QHBoxLayout()
        self.txt_folder = QLineEdit()
        self.txt_folder.setPlaceholderText('Leave empty for temporary layers…')
        folder_row.addWidget(self.txt_folder)
        btn_folder = QPushButton('Browse…'); btn_folder.setFixedWidth(80)
        btn_folder.clicked.connect(self._browse_folder)
        folder_row.addWidget(btn_folder)
        form2.addRow('Output folder:', folder_row)

        self.txt_prefix = QLineEdit('rorb')
        form2.addRow('File prefix:', self.txt_prefix)
        root.addWidget(grp2)

        # ── Step 3: run pipeline ─────────────────────────────────────────────
        grp3 = QGroupBox('Step 3 — Run Pipeline  (Name → Save → Check)')
        vlay3 = QVBoxLayout(grp3)

        self.btn_run = QPushButton('Run Pipeline')
        self.btn_run.setFixedHeight(34)
        self.btn_run.clicked.connect(self._on_run)
        vlay3.addWidget(self.btn_run)

        # Horizontal body: log (left, stretches) | stats sidebar (right, fixed)
        h_body = QHBoxLayout()
        h_body.setSpacing(6)

        # ── log column ───────────────────────────────────────────────────────
        log_col = QVBoxLayout()
        log_col.setSpacing(4)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMinimumHeight(80)
        self.txt_log.setMaximumHeight(120)
        self.txt_log.setPlaceholderText('Pipeline output will appear here…')
        self.txt_log.setFont(QFont('Consolas', 9))
        log_col.addWidget(self.txt_log)

        self.lbl_status = QLabel('')
        self.lbl_status.setAlignment(ALIGN_CENTER)
        _sf = QFont(); _sf.setBold(True); _sf.setPointSize(10)
        self.lbl_status.setFont(_sf)
        log_col.addWidget(self.lbl_status)
        h_body.addLayout(log_col, 1)

        # ── stats sidebar ────────────────────────────────────────────────────
        side = QFrame()
        side.setFrameShape(QFrame.StyledPanel)
        side.setFixedWidth(138)
        sv = QVBoxLayout(side)
        sv.setSpacing(2)
        sv.setContentsMargins(6, 4, 6, 4)

        _t = QLabel('<b>Stats</b>')
        _t.setAlignment(ALIGN_CENTER)
        sv.addWidget(_t)

        def _hsep():
            s = QFrame()
            s.setFrameShape(QFrame.HLine)
            s.setFrameShadow(QFrame.Sunken)
            sv.addWidget(s)

        def _stat(label):
            val = QLabel(f'{label}: —')
            val.setStyleSheet('font-size:9pt; font-weight:bold; color:#333;')
            sv.addWidget(val)
            return val

        _hsep()
        self._stat_subs  = _stat('Sub-catchment')
        self._stat_jun   = _stat('Junction')
        self._stat_cen   = _stat('Centroid')
        self._stat_rch   = _stat('Reaches')
        _hsep()
        self._stat_euler = QLabel('—')
        self._stat_euler.setWordWrap(True)
        self._stat_euler.setStyleSheet('font-size:8pt; color:#333;')
        sv.addWidget(self._stat_euler)
        sv.addStretch()

        h_body.addWidget(side)
        vlay3.addLayout(h_body)
        root.addWidget(grp3)

        # ── Print node settings (right panel, hidden until pipeline runs) ───────
        self._grp_print = QGroupBox('Print Node Settings')
        self._grp_print.setVisible(False)
        self._grp_print.setFixedWidth(420)
        vlay_p = QVBoxLayout(self._grp_print)

        self.tbl_print = QTableWidget(0, 4)
        self.tbl_print.setHorizontalHeaderLabels(['Node', 'Print?', 'Code', 'Location Name'])
        self.tbl_print.setSelectionMode(QTableWidget.NoSelection)
        hdr = self.tbl_print.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_print.setColumnWidth(0, 68)
        self.tbl_print.setColumnWidth(1, 55)
        self.tbl_print.setColumnWidth(2, 68)
        vlay_p.addWidget(self.tbl_print, 1)

        p_row = QHBoxLayout()
        p_row.addStretch()
        self.btn_apply_print = QPushButton('Apply Print Settings')
        self.btn_apply_print.setToolTip(
            'Write checked print nodes and selected codes back to the confluence layer')
        self.btn_apply_print.clicked.connect(self._on_apply_print)
        p_row.addWidget(self.btn_apply_print)
        vlay_p.addLayout(p_row)
        outer.addWidget(self._grp_print)

        # ── Step 4: build ────────────────────────────────────────────────────
        grp4 = QGroupBox('Step 4 — Build RORB Control File')
        vlay4 = QVBoxLayout(grp4)

        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel('Output .cat file:'))
        self.txt_output = QLineEdit()
        self.txt_output.setPlaceholderText('Select output .cat path…')
        cat_row.addWidget(self.txt_output)
        btn_cat = QPushButton('Browse…'); btn_cat.setFixedWidth(80)
        btn_cat.clicked.connect(self._browse_cat)
        cat_row.addWidget(btn_cat)
        vlay4.addLayout(cat_row)

        catg_row = QHBoxLayout()
        catg_row.addWidget(QLabel('Output .catg file:'))
        self.txt_output_catg = QLineEdit()
        self.txt_output_catg.setPlaceholderText('Select output .catg path…')
        catg_row.addWidget(self.txt_output_catg)
        btn_catg = QPushButton('Browse…'); btn_catg.setFixedWidth(80)
        btn_catg.clicked.connect(self._browse_catg)
        catg_row.addWidget(btn_catg)
        vlay4.addLayout(catg_row)

        build_row = QHBoxLayout()
        self.btn_build = QPushButton('Build .cat')
        self.btn_build.setFixedHeight(36)
        self.btn_build.setEnabled(False)
        self.btn_build.clicked.connect(lambda: self._on_build('.cat'))
        build_row.addWidget(self.btn_build)

        self.btn_build_catg = QPushButton('Build .catg')
        self.btn_build_catg.setFixedHeight(36)
        self.btn_build_catg.setEnabled(False)
        self.btn_build_catg.clicked.connect(lambda: self._on_build('.catg'))
        build_row.addWidget(self.btn_build_catg)

        self.btn_run_rorb = QPushButton('Run RORB →')
        self.btn_run_rorb.setFixedHeight(36)
        self.btn_run_rorb.setEnabled(False)
        self.btn_run_rorb.setToolTip('Run RORB_CMD.exe against the built .catg file')
        self.btn_run_rorb.clicked.connect(self._on_run_rorb)
        build_row.addWidget(self.btn_run_rorb)
        vlay4.addLayout(build_row)
        root.addWidget(grp4)

        if not self._on_catg_built:
            btn_close = QPushButton('Close')
            btn_close.clicked.connect(self.close)
            root.addWidget(btn_close, alignment=ALIGN_RIGHT)

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Select output folder')
        if folder:
            self.txt_folder.setText(folder)

    def _browse_cat(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save RORB control file', '', 'Control Vector (*.cat)'
        )
        if path:
            self.txt_output.setText(path)

    def _browse_catg(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save RORB Graphical control file', '', 'RORB Graphical (*.catg)'
        )
        if path:
            self.txt_output_catg.setText(path)

    # ── Error highlighting ────────────────────────────────────────────────────

    def _clear_error_highlights(self):
        for h in self._error_highlights:
            del h
        self._error_highlights.clear()

    def _highlight_errors(self, layer, error_ids):
        """
        Draw red thick QgsHighlight on each feature whose 'id' is in error_ids,
        then enable ID labels (red, bold, white buffer) for those features only.
        Returns the count of highlighted features.
        """
        no_id    = '' in error_ids
        named_ids = {i for i in error_ids if i}
        canvas   = self.iface.mapCanvas()
        count    = 0

        for feat in layer.getFeatures():
            fid = str(feat['id']) if feat['id'] else ''
            if fid not in named_ids and not (no_id and fid == ''):
                continue
            h = QgsHighlight(canvas, feat, layer)
            h.setColor(QColor(210, 20, 20))
            h.setFillColor(QColor(210, 20, 20, 60))
            h.setWidth(5)
            self._error_highlights.append(h)
            count += 1

        if not count:
            return 0

        # Build label filter expression for these features
        parts = []
        if named_ids:
            quoted = ', '.join(f"'{i}'" for i in sorted(named_ids))
            parts.append(f'"id" IN ({quoted})')
        if no_id:
            parts.append('"id" IS NULL OR "id" = \'\'')

        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(1.5)
        buf.setColor(QColor(255, 255, 255))

        tf = QgsTextFormat()
        tf.setColor(QColor(200, 0, 0))
        f = QFont('Arial', 9)
        f.setBold(True)
        tf.setFont(f)
        tf.setSize(9)
        tf.setBuffer(buf)

        lbl = QgsPalLayerSettings()
        lbl.fieldName   = 'id'
        lbl.filterType  = QgsPalLayerSettings.Rule
        lbl.ruleExpression = ' OR '.join(f'({p})' for p in parts)
        lbl.setFormat(tf)

        layer.setLabeling(QgsVectorLayerSimpleLabeling(lbl))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
        return count

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _log(self, status, msg):
        icons = {'pass': ('✓', 'color:#1a7a1a;'),
                 'fail': ('✗', 'color:#c0392b;'),
                 'warn': ('⚠', 'color:#d68910;'),
                 'info': ('→', 'color:#2471a3;')}
        icon, css = icons.get(status, ('·', ''))
        self.txt_log.append(f'<span style="{css}"><b>{icon}</b> {msg}</span>')
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

    def _on_run(self):
        sub   = self.cmb_sub.currentLayer()
        cent  = self.cmb_cent.currentLayer()
        conf  = self.cmb_conf.currentLayer()
        reach = self.cmb_reach.currentLayer()

        if not all([sub, cent, conf, reach]):
            QMessageBox.warning(self, 'Missing layers',
                                'Please select all four input layers.')
            return

        folder = self.txt_folder.text().strip()
        if folder and not os.path.isdir(folder):
            QMessageBox.warning(self, 'Invalid folder',
                                'The specified output folder does not exist.')
            return

        prefix = self.txt_prefix.text().strip() or 'rorb'
        use_temp = not folder

        self.btn_run.setEnabled(False)
        self.btn_build.setEnabled(False)
        self.btn_build_catg.setEnabled(False)
        self.txt_log.clear()
        self.lbl_status.setText('')
        self._named_cents = self._named_confs = self._named_reaches = self._named_basins = None
        self._clear_error_highlights()
        self._grp_print.setVisible(False)
        self.tbl_print.setRowCount(0)
        self._stat_subs.setText('Sub-catchment: —')
        self._stat_jun.setText('Junction: —')
        self._stat_cen.setText('Centroid: —')
        self._stat_rch.setText('Reaches: —')
        self._stat_euler.setText('—')

        try:
            self._run_pipeline(sub, cent, conf, reach, folder, prefix, use_temp)
        except Exception as e:
            self._log('fail', f'Pipeline error: {e}')
            self.lbl_status.setText('Pipeline failed')
            self.lbl_status.setStyleSheet('color:#c0392b;')
        finally:
            self.btn_run.setEnabled(True)

    def _run_pipeline(self, sub, cent, conf, reach, folder, prefix, use_temp):
        # Resolve working directory
        if use_temp:
            self._cleanup_tmp()
            work_dir = tempfile.mkdtemp(prefix='rorb_pipeline_')
            self._tmp_dir = work_dir
        else:
            work_dir = folder

        p = lambda name: os.path.join(work_dir, f'{prefix}_{name}.shp')

        # ── 1. Name subcatchments ────────────────────────────────────────────
        named_subs = name_subcatchments(sub, p('subs'))
        QgsProject.instance().addMapLayer(named_subs, not use_temp)
        n_subs = named_subs.featureCount()
        self._named_basins = named_subs

        # ── 2. Name centroids ────────────────────────────────────────────────
        named_cents = name_centroids(named_subs, cent, p('centroids'))
        QgsProject.instance().addMapLayer(named_cents, not use_temp)
        self._named_cents = named_cents

        # ── 3. Name confluences ──────────────────────────────────────────────
        named_confs = name_confluences(conf, p('confluences'))
        QgsProject.instance().addMapLayer(named_confs, not use_temp)
        self._named_confs = named_confs
        self._populate_print_table(named_confs)

        # ── 4. Name reaches ──────────────────────────────────────────────────
        named_reaches, unnamed = name_reaches(named_cents, named_confs, reach, p('reaches'))
        QgsProject.instance().addMapLayer(named_reaches, not use_temp)
        self._named_reaches = named_reaches

        dest = '(temporary)' if use_temp else folder
        self._log('pass', f'Layers named  →  {dest}')
        if unnamed:
            self._log('warn', f'{len(unnamed)} reach(es) could not be named (no nearby node)')

        # ── 5. Checks ────────────────────────────────────────────────────────
        n_cents  = named_cents.featureCount()
        n_confs  = named_confs.featureCount()
        n_rch    = named_reaches.featureCount()

        self._check_results, err_reach_ids, err_node_ids = run_checks(
            named_reaches, named_cents, named_confs)
        for status, msg in self._check_results:
            if status in ('fail', 'warn'):
                self._log(status, msg)
        # passes are summarised in the sidebar; only failures/warnings appear in the log

        # Update stats sidebar
        self._update_sidebar(n_subs, n_cents, n_confs, n_rch, named_confs)

        # Highlight error features in red on the map canvas
        self._clear_error_highlights()
        if err_reach_ids:
            n = self._highlight_errors(named_reaches, err_reach_ids)
            if n:
                self._log('warn', f'{n} error reach(es) marked red on map')
        if err_node_ids:
            nc = self._highlight_errors(named_cents, err_node_ids)
            nf = self._highlight_errors(named_confs, err_node_ids)
            if nc + nf:
                self._log('warn', f'{nc + nf} error node(s) marked red on map')

        n_fail = sum(1 for s, _ in self._check_results if s == 'fail')
        n_warn = sum(1 for s, _ in self._check_results if s == 'warn')

        if n_fail == 0 and n_warn == 0:
            self.lbl_status.setText('All checks passed — ready to build')
            self.lbl_status.setStyleSheet('color:#1a7a1a;')
        elif n_fail == 0:
            self.lbl_status.setText(f'{n_warn} warning(s) — review before building')
            self.lbl_status.setStyleSheet('color:#d68910;')
        else:
            self.lbl_status.setText(f'{n_fail} check(s) failed, {n_warn} warning(s)')
            self.lbl_status.setStyleSheet('color:#c0392b;')

        self.btn_build.setEnabled(True)
        self.btn_build_catg.setEnabled(True)

        # Pre-fill .cat / .catg paths
        out_dir = folder if folder else work_dir
        if not self.txt_output.text().strip():
            self.txt_output.setText(os.path.join(out_dir, f'{prefix}.cat'))
        if not self.txt_output_catg.text().strip():
            self.txt_output_catg.setText(os.path.join(out_dir, f'{prefix}.catg'))

    # ── Print node editor ─────────────────────────────────────────────────────

    def _populate_print_table(self, named_confs):
        self.tbl_print.setRowCount(0)
        field_names = {f.name() for f in named_confs.fields()}
        has_pn  = 'print_node'  in field_names
        has_pc  = 'print_code'  in field_names
        has_out = 'out'         in field_names
        has_nm  = 'node_name'   in field_names

        feats = sorted(named_confs.getFeatures(),
                       key=lambda f: str(f['id']) if f['id'] else '')

        for feat in feats:
            row = self.tbl_print.rowCount()
            self.tbl_print.insertRow(row)

            node_attr = str(feat['id']) if feat['id'] else '?'
            is_out    = bool(int(feat['out'] or 0)) if (has_out and feat['out'] is not None) else False
            label     = f'{node_attr}  [outlet]' if is_out else node_attr

            item = QTableWidgetItem(label)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, feat.id())   # store internal QGIS fid (int)
            self.tbl_print.setItem(row, 0, item)

            chk = QCheckBox()
            pn  = int(feat['print_node'] or 0) if (has_pn and feat['print_node'] is not None) else 0
            chk.setChecked(bool(pn))

            cmb = QComboBox()
            cmb.addItems(['7', '7.1', '7.2'])
            pc  = str(feat['print_code']) if (has_pc and feat['print_code']) else '7'
            idx = cmb.findText(pc)
            cmb.setCurrentIndex(idx if idx >= 0 else 0)
            cmb.setEnabled(chk.isChecked())

            txt_nm = QLineEdit()
            nm = str(feat['node_name']) if (has_nm and feat['node_name']) else ''
            txt_nm.setText(nm)
            txt_nm.setPlaceholderText('e.g. Site gauging station')
            txt_nm.setEnabled(chk.isChecked())

            chk.stateChanged.connect(lambda state, c=cmb, t=txt_nm: (
                c.setEnabled(bool(state)), t.setEnabled(bool(state))))

            self.tbl_print.setCellWidget(row, 1, chk)
            self.tbl_print.setCellWidget(row, 2, cmb)
            self.tbl_print.setCellWidget(row, 3, txt_nm)

        self._grp_print.setVisible(True)

    def _on_apply_print(self):
        if not self._named_confs:
            return
        layer  = self._named_confs
        fields = layer.fields()
        pn_idx = fields.indexFromName('print_node')
        pc_idx = fields.indexFromName('print_code')
        nm_idx = fields.indexFromName('node_name')
        if pn_idx < 0 or pc_idx < 0:
            QMessageBox.warning(self, 'Missing fields',
                                'Confluence layer is missing print_node / print_code fields.')
            return

        layer.startEditing()
        for row in range(self.tbl_print.rowCount()):
            fid    = self.tbl_print.item(row, 0).data(Qt.UserRole)  # internal QGIS fid
            chk    = self.tbl_print.cellWidget(row, 1)
            cmb    = self.tbl_print.cellWidget(row, 2)
            txt_nm = self.tbl_print.cellWidget(row, 3)
            pn     = 1 if chk.isChecked() else 0
            pc     = cmb.currentText() if chk.isChecked() else ''
            nm     = txt_nm.text().strip() if (txt_nm and chk.isChecked()) else ''
            layer.changeAttributeValue(fid, pn_idx, pn)
            layer.changeAttributeValue(fid, pc_idx, pc)
            if nm_idx >= 0:
                layer.changeAttributeValue(fid, nm_idx, nm)
        layer.commitChanges()
        layer.triggerRepaint()
        self._log('pass', 'Print settings applied to confluence layer')

    def _update_sidebar(self, n_subs, n_cents, n_confs, n_rch, named_confs):
        # Sub-catchments with range colour
        self._stat_subs.setText(f'Sub-catchment: {n_subs}')
        if 4 <= n_subs <= 30:
            self._stat_subs.setStyleSheet(
                'font-size:9pt; font-weight:bold; color:#1a7a1a;')
        else:
            self._stat_subs.setStyleSheet(
                'font-size:9pt; font-weight:bold; color:#d68910;')

        self._stat_jun.setText(f'Junction: {n_confs}')
        self._stat_cen.setText(f'Centroid: {n_cents}')
        self._stat_rch.setText(f'Reaches: {n_rch}')

        # Euler check
        lhs, rhs = n_confs + n_cents, n_rch + 1
        if lhs == rhs:
            self._stat_euler.setText('✓ Euler OK')
            self._stat_euler.setStyleSheet(
                'font-size:8pt; font-weight:bold; color:#1a7a1a;')
        else:
            diff = lhs - rhs
            word = 'extra' if diff > 0 else 'missing'
            self._stat_euler.setText(f'✗ {abs(diff)} {word}')
            self._stat_euler.setStyleSheet(
                'font-size:8pt; font-weight:bold; color:#c0392b;')

    # ── Build ─────────────────────────────────────────────────────────────────

    def _on_build(self, ext):
        if not all([self._named_reaches, self._named_cents,
                    self._named_confs, self._named_basins]):
            QMessageBox.warning(self, 'Run pipeline first',
                                'Please run the pipeline before building.')
            return

        txt = self.txt_output if ext == '.cat' else self.txt_output_catg
        btn = self.btn_build  if ext == '.cat' else self.btn_build_catg
        output = txt.text().strip()

        if not output:
            QMessageBox.warning(self, 'No output file',
                                f'Please choose an output {ext} file path.')
            return
        if not output.endswith(ext):
            output += ext

        n_fail = sum(1 for s, _ in self._check_results if s == 'fail')
        if n_fail > 0:
            reply = QMessageBox.question(
                self, 'Checks failed',
                f'{n_fail} check(s) failed.\n\n'
                f'Building may produce an invalid {ext} file.\n\n'
                'Build anyway?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        btn.setEnabled(False)
        btn.setText('Building…')

        try:
            reach_vec = QVectorLayer(self._named_reaches)
            basin_vec = QVectorLayer(self._named_basins)
            cent_vec  = QVectorLayer(self._named_cents)
            conf_vec  = QVectorLayer(self._named_confs)

            builder = pyromb.Builder()
            tr = builder.reach(reach_vec)
            tc = builder.confluence(conf_vec)
            tb = builder.basin(cent_vec, basin_vec)

            catchment = pyromb.Catchment(tc, tb, tr)
            catchment.connect()
            traveller = pyromb.Traveller(catchment)

            with open(output, 'w') as f:
                f.write(traveller.getVector(pyromb.RORB()))

            total_area = sum(b.area for b in tb)

            da = QgsDistanceArea()
            da.setSourceCrs(self._named_basins.crs(),
                            QgsProject.instance().transformContext())
            da.setEllipsoid(QgsProject.instance().ellipsoid())
            total_qgis = sum(
                da.measureArea(feat.geometry())
                for feat in self._named_basins.getFeatures()
            )
            total_qgis_km2 = da.convertAreaMeasurement(
                total_qgis, QgsUnitTypes.AreaSquareKilometers
            )

            self._log('pass', f'RORB {ext} written → {output}')
            self._log('info',
                      f'Total catchment area — '
                      f'pyromb planar: <b>{total_area:.4f} km²</b> | '
                      f'QGIS ellipsoidal: <b>{total_qgis_km2:.4f} km²</b> '
                      f'({len(tb)} sub-catchments)')
            if ext == '.catg':
                self.btn_run_rorb.setEnabled(True)
            QMessageBox.information(self, 'Build complete',
                                    f'RORB control file written to:\n{output}\n\n'
                                    f'Total catchment area\n'
                                    f'  pyromb planar:    {total_area:.4f} km²\n'
                                    f'  QGIS ellipsoidal: {total_qgis_km2:.4f} km²')
        except Exception as e:
            QMessageBox.critical(self, 'Build failed',
                                 f'Error building {ext} file:\n\n{e}')
        finally:
            btn.setEnabled(True)
            btn.setText(f'Build {ext}')

    # ── Run RORB hand-off ───────────────────────────────────────────────────────

    def _on_run_rorb(self):
        catg = self.txt_output_catg.text().strip()
        if not catg or not os.path.isfile(catg):
            QMessageBox.warning(self, 'No .catg file',
                                'Build a .catg file before running RORB.')
            return
        if self._on_catg_built:
            self._on_catg_built(catg)
            return
        from .run_rorb_dialog import RorbRunDialog
        self._run_dialog = RorbRunDialog(
            self.iface, self, catg_path=catg,
            on_open_results=self._on_open_results)
        self._run_dialog.show()
