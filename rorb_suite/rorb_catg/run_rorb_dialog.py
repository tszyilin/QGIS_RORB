# -*- coding: utf-8 -*-

__author__ = 'Tom Norman'
__date__ = '2026-06-23'
__copyright__ = '(C) 2026 by Tom Norman'

import os
import re
import subprocess
import tempfile
import time

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QTextEdit, QFileDialog, QMessageBox,
    QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QDoubleSpinBox,
    QHeaderView, QApplication,
)
from qgis.PyQt.QtCore import Qt, QThread
from qgis.PyQt.QtGui import QFont

try:
    from qgis.PyQt.QtCore import pyqtSignal
except ImportError:
    from qgis.PyQt.QtCore import Signal as pyqtSignal

from .compat import ALIGN_RIGHT, ALIGN_CENTER


# ── RORB_CMD.exe discovery ──────────────────────────────────────────────────

_CMD_PATHS = [
    r"C:\Program Files\RORBWin\RORB_CMD.exe",
    r"C:\Program Files\RORB\RORB_CMD.exe",
    r"C:\Program Files (x86)\RORB\RORB_CMD.exe",
]


def find_rorb_cmd(hint=None):
    if hint and os.path.isfile(hint):
        return hint
    return next((p for p in _CMD_PATHS if os.path.isfile(p)), None)


# ── .catg parsing — ordered interstation-area list ──────────────────────────

def _numeric_tokens(line):
    out = []
    for tok in line.split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def _is_num(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _parse_data_table(lines, marker):
    """Parse a comma/space-separated numeric table following a line containing marker."""
    found, values = False, []
    for line in lines:
        if marker in line:
            found = True
            continue
        if found:
            if line.strip().startswith('C') or line.strip() == '':
                if values:
                    break
                continue
            for tok in re.split(r'[,\s]+', line.strip()):
                if tok == '-99':
                    return values
                try:
                    values.append(float(tok))
                except ValueError:
                    pass
    return values


def parse_catg_areas(path):
    """
    Return an ordered list of {'name', 'area_km2'} dicts — one entry per
    interstation (sub-)area, in the same order as the .catg's 'Areas, km'
    vector block (the order RORB itself expects for per-area kc/m and
    IL/CL assignment in the .par file). Zero-area "dummy" stations are
    included, not skipped, since they still occupy a slot in that order.
    """
    with open(path, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()

    basin_names = []
    in_nodes = False
    for line in lines:
        s = line.strip()
        if '#NODES' in s:
            in_nodes = True
            continue
        if any(k in s for k in ('#REACHES', '#STORAGES', '#INFLOW', 'END RORB_GE')):
            in_nodes = False
        if in_nodes and s.startswith('C'):
            body = s[1:].strip()
            if not body or not body[0].isdigit():
                continue
            nums = _numeric_tokens(body)
            if len(nums) < 7:
                continue
            is_basin = int(nums[4]) == 1
            if is_basin:
                tokens = body.split()
                name = next((t for t in tokens[4:] if not _is_num(t)),
                            f'Area{len(basin_names) + 1}')
                basin_names.append(name)

    areas = _parse_data_table(lines, 'Areas, km')

    n = max(len(basin_names), len(areas))
    result = []
    for i in range(n):
        name = basin_names[i] if i < len(basin_names) else f'Area {i + 1}'
        area = areas[i] if i < len(areas) else 0.0
        result.append({'name': name, 'area_km2': area})
    return result


# ── .par file writer ────────────────────────────────────────────────────────

def write_par_file(path, catg, stm, lumped, verbosity, lossmodel, areas_params):
    """
    areas_params: ordered list of dicts with keys kc, m, il, cl — one per
    interstation area, same order as parse_catg_areas(). All field values
    must start at column 11 (cols 1-10 are free for comments); '# BEGIN'
    and '# END' must start at column 1 — both enforced by the fixed-width
    labels below, per RORB_CMD-documentation_V2.pdf.
    """
    lines = ['# BEGIN',
             f'Cat file :{catg}',
             f'Stm file :{stm}',
             f'Lumped kc:{"T" if lumped else "F"}',
             f'Verbosity:{verbosity}',
             f'Lossmodel:{lossmodel}',
             f'Num ISA  :{len(areas_params)}']

    if lumped:
        kc, m = areas_params[0]['kc'], areas_params[0]['m']
        lines.append(f'ISA 1    :{kc:.4f},{m:.4f}')
    else:
        for i, p in enumerate(areas_params, 1):
            lines.append(f'ISA {i}    :{p["kc"]:.4f},{p["m"]:.4f}')

    lines.append('Num burst:1')
    for i, p in enumerate(areas_params, 1):
        lines.append(f'ISA {i}    :{p["il"]:.4f},{p["cl"]:.4f}')

    lines.append('# END')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


# ── Output file discovery ───────────────────────────────────────────────────

def _newest_file_since(directory, ext, since_ts):
    candidates = []
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for fn in names:
        if not fn.lower().endswith(ext):
            continue
        full = os.path.join(directory, fn)
        try:
            if os.path.getmtime(full) >= since_ts:
                candidates.append(full)
        except OSError:
            pass
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


# ── Background worker — keeps the UI thread responsive during the run ──────

class _RunWorker(QThread):
    done = pyqtSignal(bool, str, str)   # ok, log_text, out_file_path

    def __init__(self, exe, par_path, watch_dir, since_ts, timeout=300):
        super().__init__()
        self.exe = exe
        self.par_path = par_path
        self.watch_dir = watch_dir
        self.since_ts = since_ts
        self.timeout = timeout

    def run(self):
        try:
            r = subprocess.run([self.exe, self.par_path],
                                capture_output=True, text=True,
                                timeout=self.timeout)
            text = (r.stdout or '') + (r.stderr or '')
        except Exception as e:
            self.done.emit(False, f'[RORB_CMD failed to launch: {e}]', '')
            return

        out_file = _newest_file_since(self.watch_dir, '.out', self.since_ts)
        log_file = _newest_file_since(self.watch_dir, '.log', self.since_ts)

        log_text = ''
        if log_file and os.path.isfile(log_file):
            try:
                with open(log_file, encoding='utf-8', errors='replace') as f:
                    log_text = f.read().strip()
            except OSError:
                pass

        # RORB_CMD documents success as: .out file produced, log file empty.
        ok = bool(out_file) and not log_text

        full_text = text
        if log_text:
            full_text += f'\n\n[RORB log file: {log_file}]\n{log_text}'
        self.done.emit(ok, full_text, out_file or '')


# ── Dialog ───────────────────────────────────────────────────────────────────

class RorbRunDialog(QDialog):

    def __init__(self, iface, parent=None, catg_path=None, stm_path=None,
                 on_open_results=None):
        super().__init__(parent)
        self.iface = iface
        self._on_open_results = on_open_results
        self._areas = []
        self._worker = None
        self._last_out_file = None
        self._setup_ui()

        if catg_path:
            self.txt_catg.setText(catg_path)
            self._load_catg(catg_path)
        if stm_path:
            self.txt_stm.setText(stm_path)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle('Run RORB')
        self.setMinimumWidth(640)
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── Inputs ───────────────────────────────────────────────────────────
        grp_in = QGroupBox('Inputs')
        form_in = QFormLayout(grp_in)
        form_in.setLabelAlignment(ALIGN_RIGHT)

        catg_row = QHBoxLayout()
        self.txt_catg = QLineEdit()
        self.txt_catg.setPlaceholderText('Select .catg catchment file…')
        catg_row.addWidget(self.txt_catg)
        btn_catg = QPushButton('Browse…'); btn_catg.setFixedWidth(80)
        btn_catg.clicked.connect(self._browse_catg)
        catg_row.addWidget(btn_catg)
        form_in.addRow('.catg file:', catg_row)

        stm_row = QHBoxLayout()
        self.txt_stm = QLineEdit()
        self.txt_stm.setPlaceholderText('Select .stm storm file…')
        stm_row.addWidget(self.txt_stm)
        btn_stm = QPushButton('Browse…'); btn_stm.setFixedWidth(80)
        btn_stm.clicked.connect(self._browse_stm)
        stm_row.addWidget(btn_stm)
        form_in.addRow('.stm file:', stm_row)

        exe_row = QHBoxLayout()
        self.txt_exe = QLineEdit(find_rorb_cmd() or '')
        self.txt_exe.setPlaceholderText('Path to RORB_CMD.exe…')
        exe_row.addWidget(self.txt_exe)
        btn_exe = QPushButton('Browse…'); btn_exe.setFixedWidth(80)
        btn_exe.clicked.connect(self._browse_exe)
        exe_row.addWidget(btn_exe)
        form_in.addRow('RORB_CMD.exe:', exe_row)

        self.lbl_exe_status = QLabel('')
        form_in.addRow('', self.lbl_exe_status)
        self._update_exe_status()

        root.addWidget(grp_in)

        # ── Run parameters ───────────────────────────────────────────────────
        grp_params = QGroupBox('Run Parameters')
        form_params = QFormLayout(grp_params)
        form_params.setLabelAlignment(ALIGN_RIGHT)

        self.cmb_verbosity = QComboBox()
        self.cmb_verbosity.addItems(['1', '2', '3'])
        self.cmb_verbosity.setCurrentText('3')
        form_params.addRow('Verbosity:', self.cmb_verbosity)

        self.cmb_lossmodel = QComboBox()
        self.cmb_lossmodel.addItems([
            'Initial loss / Continuing loss',
            'Initial loss / Runoff coefficient',
        ])
        form_params.addRow('Loss model:', self.cmb_lossmodel)

        self.chk_lumped = QCheckBox('Use one kc / m for all interstation areas')
        self.chk_lumped.setChecked(True)
        self.chk_lumped.stateChanged.connect(self._on_lumped_changed)
        form_params.addRow('Lumped kc:', self.chk_lumped)

        root.addWidget(grp_params)

        # ── Per-area table ───────────────────────────────────────────────────
        grp_areas = QGroupBox('Interstation Areas  (includes zero-area "dummy" stations)')
        vlay_areas = QVBoxLayout(grp_areas)

        fill_row = QHBoxLayout()
        fill_row.addWidget(QLabel('Fill all IL:'))
        self.spn_fill_il = QDoubleSpinBox(); self.spn_fill_il.setRange(0, 1000); self.spn_fill_il.setValue(20.0)
        fill_row.addWidget(self.spn_fill_il)
        btn_fill_il = QPushButton('Apply'); btn_fill_il.clicked.connect(lambda: self._fill_column('il', self.spn_fill_il.value()))
        fill_row.addWidget(btn_fill_il)

        fill_row.addSpacing(20)
        fill_row.addWidget(QLabel('Fill all CL:'))
        self.spn_fill_cl = QDoubleSpinBox(); self.spn_fill_cl.setRange(0, 1000); self.spn_fill_cl.setValue(2.5)
        fill_row.addWidget(self.spn_fill_cl)
        btn_fill_cl = QPushButton('Apply'); btn_fill_cl.clicked.connect(lambda: self._fill_column('cl', self.spn_fill_cl.value()))
        fill_row.addWidget(btn_fill_cl)
        fill_row.addStretch()
        vlay_areas.addLayout(fill_row)

        self.table_areas = QTableWidget(0, 6)
        self.table_areas.setHorizontalHeaderLabels(
            ['Area', 'Area (km²)', 'kc', 'm', 'IL (mm)', 'CL (mm/hr)'])
        self.table_areas.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table_areas.setMinimumHeight(160)
        vlay_areas.addWidget(self.table_areas)

        root.addWidget(grp_areas)

        # ── Run ──────────────────────────────────────────────────────────────
        self.btn_run = QPushButton('Run RORB')
        self.btn_run.setFixedHeight(34)
        self.btn_run.clicked.connect(self._on_run)
        root.addWidget(self.btn_run)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMinimumHeight(160)
        self.txt_log.setPlaceholderText('RORB_CMD output will appear here…')
        self.txt_log.setFont(QFont('Consolas', 9))
        root.addWidget(self.txt_log)

        self.lbl_status = QLabel('')
        self.lbl_status.setAlignment(ALIGN_CENTER)
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self.lbl_status.setFont(f)
        root.addWidget(self.lbl_status)

        btn_row = QHBoxLayout()
        self.btn_open_results = QPushButton('Open in Results Viewer')
        self.btn_open_results.setEnabled(False)
        self.btn_open_results.clicked.connect(self._open_in_results)
        btn_row.addWidget(self.btn_open_results)
        btn_row.addStretch()
        btn_close = QPushButton('Close')
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _browse_catg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB Graphical control file', '', 'RORB Graphical (*.catg)')
        if path:
            self.txt_catg.setText(path)
            self._load_catg(path)

    def _browse_stm(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB storm file', '', 'RORB Storm (*.stm)')
        if path:
            self.txt_stm.setText(path)

    def _browse_exe(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select RORB_CMD.exe', '', 'Executable (*.exe)')
        if path:
            self.txt_exe.setText(path)
            self._update_exe_status()

    def _update_exe_status(self):
        exe = self.txt_exe.text().strip()
        if exe and os.path.isfile(exe):
            self.lbl_exe_status.setText('')
        else:
            self.lbl_exe_status.setText(
                '<span style="color:#c0392b;">RORB_CMD.exe not found — browse to its location.</span>')

    # ── .catg loading → per-area table ──────────────────────────────────────

    def _load_catg(self, path):
        try:
            self._areas = parse_catg_areas(path)
        except Exception as e:
            QMessageBox.warning(self, 'Could not read .catg',
                                 f'Failed to parse interstation areas:\n\n{e}')
            self._areas = []
        self._rebuild_table()

    def _rebuild_table(self):
        self.table_areas.setRowCount(len(self._areas))
        for row, area in enumerate(self._areas):
            self.table_areas.setItem(row, 0, QTableWidgetItem(area['name']))
            item_area = QTableWidgetItem(f"{area['area_km2']:.4f}")
            item_area.setFlags(item_area.flags() & ~Qt.ItemIsEditable)
            self.table_areas.setItem(row, 1, item_area)

            spn_kc = QDoubleSpinBox(); spn_kc.setRange(0, 1000); spn_kc.setDecimals(3); spn_kc.setValue(1.5)
            spn_m = QDoubleSpinBox(); spn_m.setRange(0, 5); spn_m.setDecimals(3); spn_m.setValue(0.8)
            spn_il = QDoubleSpinBox(); spn_il.setRange(0, 1000); spn_il.setDecimals(2); spn_il.setValue(20.0)
            spn_cl = QDoubleSpinBox(); spn_cl.setRange(0, 1000); spn_cl.setDecimals(2); spn_cl.setValue(2.5)
            self.table_areas.setCellWidget(row, 2, spn_kc)
            self.table_areas.setCellWidget(row, 3, spn_m)
            self.table_areas.setCellWidget(row, 4, spn_il)
            self.table_areas.setCellWidget(row, 5, spn_cl)

        self._on_lumped_changed()

    def _on_lumped_changed(self):
        lumped = self.chk_lumped.isChecked()
        for row in range(self.table_areas.rowCount()):
            for col in (2, 3):
                w = self.table_areas.cellWidget(row, col)
                if w:
                    w.setEnabled(row == 0 or not lumped)

    def _fill_column(self, key, value):
        col = 4 if key == 'il' else 5
        for row in range(self.table_areas.rowCount()):
            w = self.table_areas.cellWidget(row, col)
            if w:
                w.setValue(value)

    def _collect_area_params(self):
        lumped = self.chk_lumped.isChecked()
        params = []
        kc0 = m0 = None
        for row in range(self.table_areas.rowCount()):
            kc = self.table_areas.cellWidget(row, 2).value()
            m = self.table_areas.cellWidget(row, 3).value()
            il = self.table_areas.cellWidget(row, 4).value()
            cl = self.table_areas.cellWidget(row, 5).value()
            if lumped:
                if row == 0:
                    kc0, m0 = kc, m
                kc, m = kc0, m0
            params.append({'kc': kc, 'm': m, 'il': il, 'cl': cl})
        return params

    # ── Run ──────────────────────────────────────────────────────────────────

    def _log(self, status, msg):
        icons = {'pass': ('✓', 'color:#1a7a1a;'),
                 'fail': ('✗', 'color:#c0392b;'),
                 'warn': ('⚠', 'color:#d68910;'),
                 'info': ('→', 'color:#2471a3;')}
        icon, css = icons.get(status, ('·', ''))
        self.txt_log.append(f'<span style="{css}"><b>{icon}</b> {msg}</span>')
        QApplication.processEvents()

    def _on_run(self):
        catg = self.txt_catg.text().strip()
        stm = self.txt_stm.text().strip()
        exe = self.txt_exe.text().strip()

        if not catg or not os.path.isfile(catg):
            QMessageBox.warning(self, 'Missing .catg', 'Please select a valid .catg file.')
            return
        if not stm or not os.path.isfile(stm):
            QMessageBox.warning(self, 'Missing .stm', 'Please select a valid .stm file.')
            return
        if not exe or not os.path.isfile(exe):
            QMessageBox.warning(self, 'RORB_CMD.exe not found',
                                 'Please browse to RORB_CMD.exe (installed with RORBwin).')
            return
        if not self._areas:
            QMessageBox.warning(self, 'No interstation areas',
                                 'No interstation areas were parsed from the .catg file.')
            return

        areas_params = self._collect_area_params()
        lumped = self.chk_lumped.isChecked()
        verbosity = int(self.cmb_verbosity.currentText())
        lossmodel = self.cmb_lossmodel.currentIndex() + 1

        par_fd, par_path = tempfile.mkstemp(suffix='.par')
        os.close(par_fd)
        write_par_file(par_path, os.path.abspath(catg), os.path.abspath(stm),
                        lumped, verbosity, lossmodel, areas_params)

        self.txt_log.clear()
        self.lbl_status.setText('')
        self.btn_open_results.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.btn_run.setText('Running…')
        self._log('info', f'Launching RORB_CMD with parameter file: {par_path}')

        watch_dir = os.path.dirname(os.path.abspath(stm))
        since_ts = time.time() - 1   # small margin for filesystem mtime resolution

        self._worker = _RunWorker(exe, par_path, watch_dir, since_ts)
        self._worker.done.connect(lambda ok, text, out_file: self._on_run_done(ok, text, out_file, par_path))
        self._worker.start()

    def _on_run_done(self, ok, text, out_file, par_path):
        try:
            os.unlink(par_path)
        except OSError:
            pass

        self.btn_run.setEnabled(True)
        self.btn_run.setText('Run RORB')

        if text:
            self.txt_log.append(f'<pre>{text}</pre>')

        if ok:
            self._last_out_file = out_file
            self._log('pass', f'RORB run completed → {out_file}')
            self.lbl_status.setText('Run succeeded')
            self.lbl_status.setStyleSheet('color:#1a7a1a;')
            self.btn_open_results.setEnabled(True)
        else:
            self._last_out_file = None
            self._log('fail', 'RORB run failed or produced no output — see log above.')
            self.lbl_status.setText('Run failed')
            self.lbl_status.setStyleSheet('color:#c0392b;')

    # ── Hand-off to Results Viewer ───────────────────────────────────────────

    def _open_in_results(self):
        if not self._last_out_file or not self._on_open_results:
            return
        folder = os.path.dirname(self._last_out_file)
        self._on_open_results(folder)
