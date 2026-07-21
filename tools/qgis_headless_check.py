#!/usr/bin/env python
"""
Headless QGIS sanity check for the RORB plugins.

Runs under the QGIS-bundled Python (see .claude/skills/qgis-test/SKILL.md)
with QT_QPA_PLATFORM=offscreen, so it can import the real qgis.core /
qgis.PyQt modules and construct the real dialogs without ever showing a
window. This catches import errors and bad widget calls that a plain
stubbed-out unit test cannot see, while staying fast enough to run on
every change.

It does NOT exercise toolbar wiring, iface.addDockWidget, or anything
that requires a live QGIS GUI session (mainWindow, map canvas, project).
That still needs the manual walkthrough described in CLAUDE.md.

Usage (see the qgis-test skill for the exact bundled-Python invocation):
    python tools/qgis_headless_check.py
"""

import os
import sys
import filecmp
import traceback

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_CATG = os.path.join(REPO_ROOT, '03_RORB_exe', 'RORBWin', 'SampleData', 'Fig6_7.catg')

sys.path.insert(0, os.path.join(REPO_ROOT, '01_RORB_catg'))
sys.path.insert(0, os.path.join(REPO_ROOT, '02_RORB_reseults_viewer'))

_results = []  # (name, ok, detail)


def check(name):
    """Decorator-less helper: run fn(), record pass/skip/fail, never raise."""
    def _wrap(fn):
        try:
            fn()
            _results.append((name, 'pass', ''))
        except SkipCheck as e:
            _results.append((name, 'skip', str(e)))
        except Exception as e:
            _results.append((name, 'fail', f'{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}'))
    return _wrap


class SkipCheck(Exception):
    pass


def main():
    # ── Qt / QGIS bootstrap ─────────────────────────────────────────────────
    from qgis.core import QgsApplication
    qgs = QgsApplication([], True)
    qgs.initQgis()

    from qgis.PyQt.QtWidgets import QMainWindow

    class _FakeIface:
        def __init__(self):
            self._win = QMainWindow()

        def mainWindow(self):
            return self._win

        def addDockWidget(self, *a, **k):
            pass

        def removeDockWidget(self, *a, **k):
            pass

    iface = _FakeIface()

    # ── Parser / writer logic against real sample data ─────────────────────
    @check('parse_catg_areas(Fig6_7.catg)')
    def _():
        from rorb_catg.run_rorb_dialog import parse_catg_areas, parse_catg_isa_count
        areas = parse_catg_areas(SAMPLE_CATG)
        names = [a['name'] for a in areas]
        vals = [a['area_km2'] for a in areas]
        assert names == ['A', 'B', 'C', 'E', 'D'], f'unexpected names: {names}'
        assert vals == [30.0, 28.0, 25.0, 35.0, 40.0], f'unexpected areas: {vals}'
        isa_count = parse_catg_isa_count(SAMPLE_CATG)
        assert isa_count == 1, f'expected 1 ISA group (all -99), got {isa_count}'
        # Real catg with 6 print-7.2 nodes + outlet → 7 ISA groups
        real_catg = os.path.join(REPO_ROOT, '03_RORB_exe', '02_input', 'catchment_A.catg')
        if os.path.isfile(real_catg):
            from rorb_catg.run_rorb_dialog import parse_catg_isa_groups
            groups = parse_catg_isa_groups(real_catg)
            assert len(groups) == 7, f'expected 7 ISA groups for catchment_A.catg, got {len(groups)}: {groups}'
            assert groups[0] == 'A-r', f'expected first group "A-r", got "{groups[0]}"'
            assert groups[-1] == 'outlet', f'expected last group "outlet", got "{groups[-1]}"'

    @check('parse_catg_calc_order(Fig6_7.catg)')
    def _():
        from rorb_catg.run_rorb_dialog import parse_catg_calc_order
        rows, missing = parse_catg_calc_order(SAMPLE_CATG)
        assert rows, 'expected at least one row from Fig6_7.catg'
        assert not any(missing[k] for k in missing), f'unexpected missing elements: {missing}'

    # ── Dialog construction (no .show(), no real GUI) ──────────────────────
    @check('construct CreateLayersDialog')
    def _():
        from rorb_catg.create_layers_dialog import CreateLayersDialog
        CreateLayersDialog(iface.mainWindow())

    @check('construct RorbPipelineDialog')
    def _():
        try:
            from rorb_catg.pipeline_dialog import RorbPipelineDialog
        except ModuleNotFoundError as e:
            if e.name == 'pyromb':
                raise SkipCheck('pyromb not installed in this Python — install '
                                 'pyromb>=0.3 to exercise this dialog') from e
            raise
        RorbPipelineDialog(iface, iface.mainWindow())

    @check('construct RorbRunDialog')
    def _():
        from rorb_catg.run_rorb_dialog import RorbRunDialog
        RorbRunDialog(iface, iface.mainWindow())

    @check('construct RorbRunDialog (pre-filled from sample .catg)')
    def _():
        from rorb_catg.run_rorb_dialog import RorbRunDialog
        dlg = RorbRunDialog(iface, iface.mainWindow(), catg_path=SAMPLE_CATG)
        # Calc. Order tab should be populated after loading a .catg
        assert dlg._calc_txt_catg.text() == SAMPLE_CATG, \
            f'expected catg path in Calc. Order tab, got "{dlg._calc_txt_catg.text()}"'
        # Fig6_7.catg has no print-7 nodes so table may be empty; just check no crash
        assert dlg._calc_table is not None, '_calc_table widget missing'

    @check('construct RorbResultsDialog')
    def _():
        from rorb_qgis.results_dialog import RorbResultsDialog
        RorbResultsDialog(iface.mainWindow())

    # ── (RORB_CMD.exe end-to-end run removed — run functionality no longer in plugin) ──

    # ── Sync-drift check: catch "forgot to copy into rorb_suite" mistakes ──
    @check('rorb_catg source == rorb_suite/rorb_catg copy')
    def _():
        src = os.path.join(REPO_ROOT, '01_RORB_catg', 'rorb_catg')
        dst = os.path.join(REPO_ROOT, 'rorb_suite', 'rorb_catg')
        _assert_dirs_match(src, dst)

    @check('rorb_qgis source == rorb_suite/rorb_qgis copy')
    def _():
        src = os.path.join(REPO_ROOT, '02_RORB_reseults_viewer', 'rorb_qgis')
        dst = os.path.join(REPO_ROOT, 'rorb_suite', 'rorb_qgis')
        _assert_dirs_match(src, dst)

    qgs.exitQgis()

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print('=' * 70)
    n_fail = sum(1 for _, status, _ in _results if status == 'fail')
    n_skip = sum(1 for _, status, _ in _results if status == 'skip')
    n_pass = sum(1 for _, status, _ in _results if status == 'pass')
    for name, status, detail in _results:
        if status == 'pass':
            print(f'  PASS  {name}')
        elif status == 'skip':
            print(f'  SKIP  {name} — {detail}')
        else:
            print(f'  FAIL  {name}')
            print('        ' + detail.replace('\n', '\n        '))
    print('=' * 70)
    print(f'{n_pass} passed, {n_fail} failed, {n_skip} skipped')
    sys.exit(1 if n_fail else 0)


def _assert_dirs_match(src, dst):
    diffs = []

    def _recurse(s, d, rel):
        cmp = filecmp.dircmp(s, d, ignore=['__pycache__'])
        if cmp.left_only:
            diffs.append(f'only in {src}: {[os.path.join(rel, f) for f in sorted(cmp.left_only)]}')
        if cmp.right_only:
            diffs.append(f'only in {dst}: {[os.path.join(rel, f) for f in sorted(cmp.right_only)]}')
        if cmp.diff_files:
            diffs.append(f'content differs: {[os.path.join(rel, f) for f in sorted(cmp.diff_files)]}')
        for sub in cmp.common_dirs:
            _recurse(os.path.join(s, sub), os.path.join(d, sub), os.path.join(rel, sub))

    _recurse(src, dst, '')
    assert not diffs, '; '.join(diffs)


if __name__ == '__main__':
    main()
