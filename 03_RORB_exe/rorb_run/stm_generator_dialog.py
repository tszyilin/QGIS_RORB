# -*- coding: utf-8 -*-
"""STM Generator dialog — two modes:
  1. ARR2016 design storms (IFD CSV + temporal patterns + hub → many .stm files)
  2. Custom time series (user time/rainfall CSV → single .stm file)
"""

import csv as _csv_mod
import os
import time

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QTextEdit, QFileDialog,
    QComboBox, QSpinBox, QDoubleSpinBox, QTabWidget, QWidget,
    QRadioButton, QButtonGroup, QCheckBox, QApplication,
)
from qgis.PyQt.QtCore import Qt, QThread, QSettings

try:
    from qgis.PyQt.QtCore import pyqtSignal
except ImportError:
    from qgis.PyQt.QtCore import Signal as pyqtSignal

from . import arr2016_runner as _arr

# ── AEP / Duration lists ──────────────────────────────────────────────────────

_AEPS = [
    '12EY', '6EY', '4EY', '3EY', '2EY', '63.2%', '50%', '0.5EY',
    '20%', '10%', '5%', '2%', '1%', '0.2EY',
    '1 in 200', '1 in 500', '1 in 1000', '1 in 2000',
]

_DURATIONS = [
    '1 min', '2 min', '3 min', '5 min', '10 min', '15 min', '30 min',
    '1 hr', '2 hr', '3 hr', '6 hr', '12 hr', '24 hr',
    '48 hr', '72 hr', '96 hr', '120 hr', '144 hr', '168 hr',
]

_DUR_TO_MIN = {
    '1 min': 1, '2 min': 2, '3 min': 3, '5 min': 5, '10 min': 10,
    '15 min': 15, '30 min': 30, '1 hr': 60, '2 hr': 120, '3 hr': 180,
    '6 hr': 360, '12 hr': 720, '24 hr': 1440, '48 hr': 2880, '72 hr': 4320,
    '96 hr': 5760, '120 hr': 7200, '144 hr': 8640, '168 hr': 10080,
}


# ── ARR2016 worker ────────────────────────────────────────────────────────────

class _ArrStmWorker(QThread):
    progress = pyqtSignal(str)
    done     = pyqtSignal(int, int)   # n_ok, n_total

    def __init__(self, depths_csv, rinc_csv, hub_txt,
                 aep_list, dur_min_list,
                 area_km2, n_subareas, out_dir, prefix):
        super().__init__()
        self.depths_csv  = depths_csv
        self.rinc_csv    = rinc_csv
        self.hub_txt     = hub_txt
        self.aep_list    = aep_list
        self.dur_min_list = dur_min_list
        self.area_km2    = area_km2
        self.n_subareas  = n_subareas
        self.out_dir     = out_dir
        self.prefix      = prefix
        self._stop       = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self._execute()
        except Exception as e:
            self.progress.emit(f'[ERROR] Unexpected: {e}')
            self.done.emit(0, 0)

    def _execute(self):
        os.makedirs(self.out_dir, exist_ok=True)

        try:
            depths, _ = _arr.load_depths(self.depths_csv)
        except Exception as e:
            self.progress.emit(f'[ERROR] Cannot load IFD depths: {e}')
            self.done.emit(0, 0)
            return

        try:
            patterns, areal_areas = _arr.load_patterns_v2(self.rinc_csv)
        except Exception as e:
            self.progress.emit(f'[ERROR] Cannot load temporal patterns: {e}')
            self.done.emit(0, 0)
            return

        is_areal   = areal_areas is not None
        areal_area = None
        if is_areal:
            areal_area = _arr.select_standard_area(areal_areas, self.area_km2)
            self.progress.emit(
                f'[INFO] Areal patterns detected — catchment {self.area_km2:.1f} km², '
                f'selected standard area {areal_area:.0f} km² '
                f'(available: {[int(a) for a in areal_areas]})')
            from collections import defaultdict as _dd
            sel = _dd(list)
            for (dur, area), evts in patterns.items():
                if area == areal_area:
                    sel[(dur, '_areal_')] = evts
            patterns = sel

        arf_params = None
        if self.hub_txt:
            try:
                arf_params = _arr.load_arf_params(self.hub_txt)
            except Exception:
                pass

        common_durs = sorted(
            set(self.dur_min_list) & set(depths.keys()) & {k[0] for k in patterns}
        )
        if not common_durs:
            self.progress.emit('[ERROR] No common durations between IFD CSV and temporal patterns CSV.')
            self.done.emit(0, 0)
            return

        self.progress.emit(f'[INFO] Common durations: {common_durs} min')

        # Build run list
        runs = []
        for aep in self.aep_list:
            cls = _arr.aep_to_class(aep)
            if cls is None:
                self.progress.emit(f'[WARN] AEP "{aep}" not recognised — skipped')
                continue
            for dur_min in common_durs:
                depth_mm = depths.get(dur_min, {}).get(aep)
                if depth_mm is None:
                    continue
                arf  = _arr.calc_arf(arf_params, dur_min, self.area_km2,
                                     _arr.aep_label_to_fraction(aep))
                key  = (dur_min, cls)
                pats = sorted(patterns.get(key, []), key=lambda x: x[0])
                if not pats and cls == 'very rare':
                    pats = sorted(patterns.get((dur_min, 'rare'), []), key=lambda x: x[0])
                if not pats:
                    pats = sorted(patterns.get((dur_min, '_areal_'), []), key=lambda x: x[0])
                for pos, (event_id, ts_min, fracs) in enumerate(pats, 1):
                    tp_num = pos if is_areal else _arr.tp_number(aep, pos)
                    runs.append({
                        'aep': aep, 'dur_min': dur_min, 'tp_num': tp_num,
                        'ts_min': ts_min, 'fracs': fracs,
                        'depth_mm': depth_mm, 'arf': arf,
                    })

        total = len(runs)
        if total == 0:
            self.progress.emit('[WARN] No runs — check AEP/duration selections match CSV data.')
            self.done.emit(0, 0)
            return

        n_ok = 0
        t0   = time.time()
        prefix = self.prefix.strip() or 'storm'

        for i, run in enumerate(runs):
            if self._stop:
                break
            aep_s  = _arr.aep_filename_label(run['aep'])
            dur_s  = _arr.dur_label(run['dur_min'])
            fname  = f'{prefix}_aep{aep_s}_du{dur_s}tp{run["tp_num"]}.stm'
            out_path = os.path.join(self.out_dir, fname)

            try:
                _arr.write_stm(
                    out_path, run['fracs'], run['ts_min'],
                    run['depth_mm'], run['arf'],
                    catg_name=prefix,
                    aep=run['aep'],
                    dur_display_str=_arr.dur_display(run['dur_min']),
                    tp_num=run['tp_num'],
                    area_km2=self.area_km2,
                    n_subareas=self.n_subareas,
                    is_areal=is_areal,
                    standard_area_km2=areal_area,
                )
                n_ok += 1
            except OSError as e:
                self.progress.emit(f'[WARN] {fname}: {e}')
                continue

            elapsed = time.time() - t0
            pct = (i + 1) / total * 100
            self.progress.emit(
                f'[{i+1}/{total} {pct:.0f}%]  {fname}  '
                f'depth={run["depth_mm"] * run["arf"]:.1f}mm  ({elapsed:.0f}s)'
            )

        self.progress.emit(f'Done: {n_ok}/{total} .stm files written to {self.out_dir}')
        self.done.emit(n_ok, total)


# ── Helper: browse button row ─────────────────────────────────────────────────

def _browse_row(parent, label, placeholder, file_filter=None, save=False):
    """Return (QWidget row, QLineEdit) for a labelled file-browse row."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    lay.addWidget(edit)
    btn = QPushButton('Browse…')
    btn.setFixedWidth(72)
    def _browse():
        if save:
            path, _ = QFileDialog.getSaveFileName(parent, label, '', file_filter or '')
        else:
            path, _ = QFileDialog.getOpenFileName(parent, label, '', file_filter or '')
        if path:
            edit.setText(path)
    btn.clicked.connect(_browse)
    lay.addWidget(btn)
    return row, edit


def _browse_dir_row(parent, label, placeholder):
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    lay.addWidget(edit)
    btn = QPushButton('Browse…')
    btn.setFixedWidth(72)
    def _browse():
        path = QFileDialog.getExistingDirectory(parent, label)
        if path:
            edit.setText(path)
    btn.clicked.connect(_browse)
    lay.addWidget(btn)
    return row, edit


# ── Dialog ────────────────────────────────────────────────────────────────────

class StmGeneratorDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Generate STM Files')
        self.setMinimumWidth(620)
        self.setMinimumHeight(560)
        self._worker = None
        self._setup_ui()
        self._restore_settings()

    def closeEvent(self, event):
        self._save_settings()
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        super().closeEvent(event)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _save_settings(self):
        s = QSettings('RORB', 'StmGenerator')
        s.setValue('ifd_path',      self._ifd_edit.text())
        s.setValue('tp_path',       self._tp_edit.text())
        s.setValue('hub_path',      self._hub_edit.text())
        s.setValue('area_km2',      self._area_spn.value())
        s.setValue('n_subareas',    self._nsub_spn.value())
        s.setValue('prefix',        self._prefix_edit.text())
        s.setValue('out_dir',       self._outdir_edit.text())
        s.setValue('aep_from',      self._aep_from.currentText())
        s.setValue('aep_to',        self._aep_to.currentText())
        s.setValue('dur_from',      self._dur_from.currentText())
        s.setValue('dur_to',        self._dur_to.currentText())
        s.setValue('ts_csv',        self._ts_csv_edit.text())
        s.setValue('ts_out',        self._ts_out_edit.text())
        s.setValue('ts_name',       self._ts_name_edit.text())
        s.setValue('ts_nsub',       self._ts_nsub_spn.value())

    def _restore_settings(self):
        s = QSettings('RORB', 'StmGenerator')
        def _t(edit, key): edit.setText(s.value(key, ''))
        _t(self._ifd_edit,      'ifd_path')
        _t(self._tp_edit,       'tp_path')
        _t(self._hub_edit,      'hub_path')
        _t(self._prefix_edit,   'prefix')
        _t(self._outdir_edit,   'out_dir')
        _t(self._ts_csv_edit,   'ts_csv')
        _t(self._ts_out_edit,   'ts_out')
        _t(self._ts_name_edit,  'ts_name')
        self._area_spn.setValue(s.value('area_km2', 100.0, type=float))
        self._nsub_spn.setValue(s.value('n_subareas', 1, type=int))
        self._ts_nsub_spn.setValue(s.value('ts_nsub', 1, type=int))
        for cmb, key in [(self._aep_from, 'aep_from'), (self._aep_to, 'aep_to'),
                         (self._dur_from, 'dur_from'), (self._dur_to, 'dur_to')]:
            v = s.value(key, '')
            if v and cmb.findText(v) >= 0:
                cmb.setCurrentText(v)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        tabs = QTabWidget()
        tabs.addTab(self._build_arr_tab(),  'ARR2016 Design Storms')
        tabs.addTab(self._build_ts_tab(),   'Custom Time Series')
        root.addWidget(tabs)

        root.addWidget(self._build_log())

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton('Close')
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    # ── Tab 1: ARR2016 ────────────────────────────────────────────────────────

    def _build_arr_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(6)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        hub_row, self._hub_edit = _browse_row(self, 'ARR hub .txt', 'optional — for long-duration ARF',
                                               'ARR Data Hub (*.txt)')
        form.addRow('ARR hub .txt:', hub_row)

        ifd_row, self._ifd_edit = _browse_row(self, 'IFD depths CSV', 'depths_…_ifds_….csv',
                                               'CSV (*.csv)')
        form.addRow('IFD CSV:', ifd_row)

        tp_row, self._tp_edit = _browse_row(self, 'Temporal patterns CSV', 'R_Increments.csv',
                                             'CSV (*.csv)')
        form.addRow('Temporal patterns CSV:', tp_row)

        area_row = QWidget()
        al = QHBoxLayout(area_row); al.setContentsMargins(0, 0, 0, 0)
        self._area_spn = QDoubleSpinBox()
        self._area_spn.setRange(0.1, 1e6)
        self._area_spn.setDecimals(1)
        self._area_spn.setValue(100.0)
        self._area_spn.setSuffix(' km²')
        self._area_spn.setFixedWidth(110)
        al.addWidget(self._area_spn)
        al.addWidget(QLabel('   Sub-areas:'))
        self._nsub_spn = QSpinBox()
        self._nsub_spn.setRange(1, 9999)
        self._nsub_spn.setValue(1)
        self._nsub_spn.setFixedWidth(70)
        al.addWidget(self._nsub_spn)
        al.addStretch()
        form.addRow('Catchment area:', area_row)

        aep_row = QWidget()
        rl = QHBoxLayout(aep_row); rl.setContentsMargins(0, 0, 0, 0)
        self._aep_from = QComboBox(); self._aep_from.addItems(_AEPS)
        self._aep_to   = QComboBox(); self._aep_to.addItems(_AEPS)
        self._aep_to.setCurrentText('1%')
        rl.addWidget(QLabel('From')); rl.addWidget(self._aep_from)
        rl.addWidget(QLabel('  to')); rl.addWidget(self._aep_to)
        rl.addStretch()
        form.addRow('AEP range:', aep_row)

        dur_row = QWidget()
        dl = QHBoxLayout(dur_row); dl.setContentsMargins(0, 0, 0, 0)
        self._dur_from = QComboBox(); self._dur_from.addItems(_DURATIONS)
        self._dur_from.setCurrentText('1 hr')
        self._dur_to   = QComboBox(); self._dur_to.addItems(_DURATIONS)
        self._dur_to.setCurrentText('72 hr')
        dl.addWidget(QLabel('From')); dl.addWidget(self._dur_from)
        dl.addWidget(QLabel('  to')); dl.addWidget(self._dur_to)
        dl.addStretch()
        form.addRow('Duration range:', dur_row)

        prefix_w = QWidget()
        pl = QHBoxLayout(prefix_w); pl.setContentsMargins(0, 0, 0, 0)
        self._prefix_edit = QLineEdit()
        self._prefix_edit.setPlaceholderText('e.g. catchment_A')
        pl.addWidget(self._prefix_edit)
        form.addRow('Storm prefix:', prefix_w)

        outdir_row, self._outdir_edit = _browse_dir_row(self, 'Output folder', 'folder to write .stm files into')
        form.addRow('Output folder:', outdir_row)

        lay.addLayout(form)

        btn_row = QHBoxLayout()
        self._arr_btn_gen = QPushButton('Generate STM Files')
        self._arr_btn_gen.setMinimumHeight(32)
        self._arr_btn_gen.clicked.connect(self._on_arr_generate)
        btn_row.addWidget(self._arr_btn_gen)
        self._arr_btn_stop = QPushButton('Stop')
        self._arr_btn_stop.setVisible(False)
        self._arr_btn_stop.clicked.connect(self._on_stop)
        btn_row.addWidget(self._arr_btn_stop)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        lay.addStretch()
        return w

    # ── Tab 2: Custom time series ─────────────────────────────────────────────

    def _build_ts_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(6)

        grp_input = QGroupBox('Input CSV')
        grp_lay = QFormLayout(grp_input)
        grp_lay.setLabelAlignment(Qt.AlignRight)

        csv_row = QWidget()
        cl = QHBoxLayout(csv_row); cl.setContentsMargins(0, 0, 0, 0)
        self._ts_csv_edit = QLineEdit()
        self._ts_csv_edit.setPlaceholderText('time/rainfall CSV file')
        cl.addWidget(self._ts_csv_edit)
        btn_csv = QPushButton('Browse…'); btn_csv.setFixedWidth(72)
        btn_csv.clicked.connect(self._ts_browse_csv)
        cl.addWidget(btn_csv)
        btn_load = QPushButton('Load'); btn_load.setFixedWidth(50)
        btn_load.clicked.connect(self._ts_load_csv)
        cl.addWidget(btn_load)
        grp_lay.addRow('CSV file:', csv_row)

        self._ts_time_col = QComboBox()
        grp_lay.addRow('Time column:', self._ts_time_col)

        self._ts_rf_col = QComboBox()
        grp_lay.addRow('Rainfall column:', self._ts_rf_col)

        unit_row = QWidget()
        ul = QHBoxLayout(unit_row); ul.setContentsMargins(0, 0, 0, 0)
        self._ts_unit_min = QRadioButton('minutes')
        self._ts_unit_hr  = QRadioButton('hours')
        self._ts_unit_min.setChecked(True)
        ul.addWidget(self._ts_unit_min); ul.addWidget(self._ts_unit_hr); ul.addStretch()
        grp_lay.addRow('Time units:', unit_row)

        type_row = QWidget()
        tl = QHBoxLayout(type_row); tl.setContentsMargins(0, 0, 0, 0)
        self._ts_incr = QRadioButton('Incremental (mm per step)')
        self._ts_cum  = QRadioButton('Cumulative (mm total)')
        self._ts_incr.setChecked(True)
        tl.addWidget(self._ts_incr); tl.addWidget(self._ts_cum); tl.addStretch()
        grp_lay.addRow('Rainfall type:', type_row)

        lay.addWidget(grp_input)

        grp_out = QGroupBox('Output')
        ol = QFormLayout(grp_out)
        ol.setLabelAlignment(Qt.AlignRight)

        name_w = QWidget(); nl = QHBoxLayout(name_w); nl.setContentsMargins(0, 0, 0, 0)
        self._ts_name_edit = QLineEdit(); self._ts_name_edit.setPlaceholderText('Storm_01')
        nl.addWidget(self._ts_name_edit)
        ol.addRow('Storm name:', name_w)

        nsub_w = QWidget(); sl = QHBoxLayout(nsub_w); sl.setContentsMargins(0, 0, 0, 0)
        self._ts_nsub_spn = QSpinBox(); self._ts_nsub_spn.setRange(1, 9999); self._ts_nsub_spn.setValue(1)
        self._ts_nsub_spn.setFixedWidth(70)
        sl.addWidget(self._ts_nsub_spn); sl.addStretch()
        ol.addRow('Sub-areas:', nsub_w)

        out_row = QWidget(); ow = QHBoxLayout(out_row); ow.setContentsMargins(0, 0, 0, 0)
        self._ts_out_edit = QLineEdit(); self._ts_out_edit.setPlaceholderText('output.stm')
        ow.addWidget(self._ts_out_edit)
        btn_out = QPushButton('Browse…'); btn_out.setFixedWidth(72)
        btn_out.clicked.connect(lambda: self._ts_browse_out())
        ow.addWidget(btn_out)
        ol.addRow('Output .stm:', out_row)

        lay.addWidget(grp_out)

        gen_btn_row = QHBoxLayout()
        self._ts_btn_gen = QPushButton('Generate STM File')
        self._ts_btn_gen.setMinimumHeight(32)
        self._ts_btn_gen.clicked.connect(self._on_ts_generate)
        gen_btn_row.addWidget(self._ts_btn_gen)
        gen_btn_row.addStretch()
        lay.addLayout(gen_btn_row)
        lay.addStretch()
        return w

    # ── Log area ──────────────────────────────────────────────────────────────

    def _build_log(self):
        grp = QGroupBox('Log')
        gl = QVBoxLayout(grp)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)
        self._log.setFont(__import__('qgis.PyQt.QtGui', fromlist=['QFont']).QFont('Courier New', 8))
        gl.addWidget(self._log)
        return grp

    def _log_append(self, msg):
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
        QApplication.processEvents()

    # ── ARR2016 generate ─────────────────────────────────────────────────────

    def _aep_range(self):
        i0 = _AEPS.index(self._aep_from.currentText())
        i1 = _AEPS.index(self._aep_to.currentText())
        if i0 > i1:
            i0, i1 = i1, i0
        return _AEPS[i0:i1 + 1]

    def _dur_range(self):
        i0 = _DURATIONS.index(self._dur_from.currentText())
        i1 = _DURATIONS.index(self._dur_to.currentText())
        if i0 > i1:
            i0, i1 = i1, i0
        return [_DUR_TO_MIN[d] for d in _DURATIONS[i0:i1 + 1]]

    def _on_arr_generate(self):
        ifd = self._ifd_edit.text().strip()
        tp  = self._tp_edit.text().strip()
        out = self._outdir_edit.text().strip()
        if not ifd or not os.path.isfile(ifd):
            self._log_append('[ERROR] IFD CSV not found.')
            return
        if not tp or not os.path.isfile(tp):
            self._log_append('[ERROR] Temporal patterns CSV not found.')
            return
        if not out:
            self._log_append('[ERROR] Output folder not set.')
            return

        hub = self._hub_edit.text().strip() or None
        if hub and not os.path.isfile(hub):
            self._log_append('[WARN] ARR hub .txt not found — ARF will be 1.0 for long durations.')
            hub = None

        self._log.clear()
        self._arr_btn_gen.setEnabled(False)
        self._arr_btn_stop.setVisible(True)

        self._worker = _ArrStmWorker(
            depths_csv=ifd, rinc_csv=tp, hub_txt=hub,
            aep_list=self._aep_range(),
            dur_min_list=self._dur_range(),
            area_km2=self._area_spn.value(),
            n_subareas=self._nsub_spn.value(),
            out_dir=out,
            prefix=self._prefix_edit.text().strip() or 'storm',
        )
        self._worker.progress.connect(self._log_append)
        self._worker.done.connect(self._on_arr_done)
        self._worker.start()

    def _on_stop(self):
        if self._worker:
            self._worker.stop()

    def _on_arr_done(self, n_ok, n_total):
        self._arr_btn_gen.setEnabled(True)
        self._arr_btn_stop.setVisible(False)
        if n_total:
            self._log_append(f'[DONE] {n_ok}/{n_total} files generated.')

    # ── Custom time series generate ───────────────────────────────────────────

    def _ts_browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open time/rainfall CSV', '', 'CSV (*.csv)')
        if path:
            self._ts_csv_edit.setText(path)
            self._ts_load_csv()

    def _ts_browse_out(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save STM file', '', 'STM (*.stm)')
        if path:
            self._ts_out_edit.setText(path)

    def _ts_load_csv(self):
        path = self._ts_csv_edit.text().strip()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                reader = _csv_mod.reader(f)
                headers = next(reader)
            cols = [h.strip() for h in headers if h.strip()]
            self._ts_time_col.clear()
            self._ts_rf_col.clear()
            self._ts_time_col.addItems(cols)
            self._ts_rf_col.addItems(cols)
            if len(cols) > 1:
                self._ts_rf_col.setCurrentIndex(1)
            self._log_append(f'[INFO] Loaded columns: {cols}')
        except Exception as e:
            self._log_append(f'[ERROR] Cannot read CSV: {e}')

    def _on_ts_generate(self):
        path = self._ts_csv_edit.text().strip()
        out  = self._ts_out_edit.text().strip()
        if not path or not os.path.isfile(path):
            self._log_append('[ERROR] Input CSV not found.')
            return
        if not out:
            self._log_append('[ERROR] Output .stm path not set.')
            return

        time_col = self._ts_time_col.currentText()
        rf_col   = self._ts_rf_col.currentText()
        if not time_col or not rf_col:
            self._log_append('[ERROR] Load the CSV first to select columns.')
            return

        try:
            times, rainfalls = [], []
            with open(path, encoding='utf-8', errors='replace') as f:
                reader = _csv_mod.DictReader(f)
                for row in reader:
                    t = row.get(time_col, '').strip()
                    r = row.get(rf_col,   '').strip()
                    if t and r:
                        times.append(float(t))
                        rainfalls.append(float(r))
        except Exception as e:
            self._log_append(f'[ERROR] Cannot parse CSV: {e}')
            return

        if len(times) < 2:
            self._log_append('[ERROR] Need at least 2 rows of data.')
            return

        # Convert to incremental if cumulative
        if self._ts_cum.isChecked():
            rainfalls = [rainfalls[i] - rainfalls[i-1] for i in range(1, len(rainfalls))]
            times = times[1:]

        # Time step
        dt = times[1] - times[0]
        if not self._ts_unit_min.isChecked():
            dt *= 60.0   # hours → minutes
        ts_min = max(1, round(dt))

        total_mm = sum(rainfalls)
        if total_mm <= 0:
            self._log_append('[ERROR] Total rainfall is zero or negative.')
            return

        fracs = [r / total_mm * 100.0 for r in rainfalls]
        name  = self._ts_name_edit.text().strip() or 'Custom_Storm'

        try:
            _arr.write_stm(
                out, fracs, ts_min, total_mm, 1.0,
                catg_name=name,
                aep='User-defined',
                dur_display_str=f'{len(fracs) * ts_min} min total',
                tp_num=1,
                area_km2=0.0,
                n_subareas=self._ts_nsub_spn.value(),
            )
            self._log_append(f'[DONE] Written: {out}')
            self._log_append(f'       {len(fracs)} steps × {ts_min} min  total={total_mm:.1f} mm')
        except OSError as e:
            self._log_append(f'[ERROR] {e}')
