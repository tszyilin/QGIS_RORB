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
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont

from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsProject, QgsDistanceArea, QgsUnitTypes
from .compat import (ALIGN_RIGHT, ALIGN_CENTER,
                     LAYER_POINT, LAYER_LINE, LAYER_POLYGON)

from .pipeline_utils import (
    name_subcatchments, name_centroids, name_confluences,
    name_reaches, run_checks,
)
from .custom_types.qvector_layer import QVectorLayer
try:
    import pyromb
except ImportError:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'vendor'))
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
        self._check_results = []
        self._tmp_dir       = None
        self._run_dialog    = None
        self._setup_ui()

    def closeEvent(self, event):
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
        root = QVBoxLayout(self)
        root.setSpacing(10)

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

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMinimumHeight(200)
        self.txt_log.setPlaceholderText('Pipeline output will appear here…')
        self.txt_log.setFont(QFont('Consolas', 9))
        vlay3.addWidget(self.txt_log)

        self.lbl_status = QLabel('')
        self.lbl_status.setAlignment(ALIGN_CENTER)
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self.lbl_status.setFont(f)
        vlay3.addWidget(self.lbl_status)
        root.addWidget(grp3)

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
            self._log('info', 'No folder specified — saving named layers as temporary')
        else:
            work_dir = folder

        p = lambda name: os.path.join(work_dir, f'{prefix}_{name}.shp')

        # ── 1. Name subcatchments ────────────────────────────────────────────
        self._log('info', 'Naming subcatchments south → north…')
        named_subs = name_subcatchments(sub, p('subs'))
        QgsProject.instance().addMapLayer(named_subs, not use_temp)
        lbl = '(temporary)' if use_temp else p('subs')
        self._log('pass', f'Subcatchments named → {lbl}')
        self._named_basins = named_subs

        # ── 2. Name centroids ────────────────────────────────────────────────
        self._log('info', 'Naming centroids…')
        named_cents = name_centroids(named_subs, cent, p('centroids'))
        QgsProject.instance().addMapLayer(named_cents, not use_temp)
        lbl = '(temporary)' if use_temp else p('centroids')
        self._log('pass', f'Centroids named → {lbl}')
        self._named_cents = named_cents

        # ── 3. Name confluences ──────────────────────────────────────────────
        self._log('info', 'Naming confluences south → north…')
        named_confs = name_confluences(conf, p('confluences'))
        QgsProject.instance().addMapLayer(named_confs, not use_temp)
        lbl = '(temporary)' if use_temp else p('confluences')
        self._log('pass', f'Confluences named → {lbl}')
        self._named_confs = named_confs

        # ── 4. Name reaches ──────────────────────────────────────────────────
        self._log('info', 'Naming reaches…')
        named_reaches, unnamed = name_reaches(named_cents, named_confs, reach, p('reaches'))
        QgsProject.instance().addMapLayer(named_reaches, not use_temp)
        lbl = '(temporary)' if use_temp else p('reaches')
        self._log('pass', f'Reaches named → {lbl}')
        if unnamed:
            self._log('warn', f'{len(unnamed)} reach(es) could not be named (no nearby node)')
        self._named_reaches = named_reaches

        # ── 5. Checks ────────────────────────────────────────────────────────
        self._log('info', '─── Running link checks ───')
        self._check_results, err_reach_ids, err_node_ids = run_checks(
            named_reaches, named_cents, named_confs)
        for status, msg in self._check_results:
            self._log(status, msg)

        # Select error features in the map so the user can see them immediately
        named_reaches.removeSelection()
        named_cents.removeSelection()
        named_confs.removeSelection()

        if err_reach_ids:
            parts = []
            no_id = '' in err_reach_ids or None in err_reach_ids
            named_ids = {i for i in err_reach_ids if i and i != 'None'}
            if named_ids:
                quoted = ', '.join(f"'{i}'" for i in sorted(named_ids))
                parts.append(f'"id" IN ({quoted})')
            if no_id:
                parts.append('"id" IS NULL OR "id" = \'\'')
            if parts:
                named_reaches.selectByExpression(' OR '.join(f'({p})' for p in parts))
            n = named_reaches.selectedFeatureCount()
            if n:
                self._log('warn', f'{n} error reach(es) selected in map (shown in yellow)')

        if err_node_ids:
            quoted = ', '.join(f"'{i}'" for i in sorted(err_node_ids))
            expr = f'"id" IN ({quoted})'
            named_cents.selectByExpression(expr)
            named_confs.selectByExpression(expr)
            n = named_cents.selectedFeatureCount() + named_confs.selectedFeatureCount()
            if n:
                self._log('warn', f'{n} error node(s) selected in map (shown in yellow)')

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
