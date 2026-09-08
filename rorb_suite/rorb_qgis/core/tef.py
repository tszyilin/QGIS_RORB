# -*- coding: utf-8 -*-
"""TUFLOW Event File (.tef) parser + filename-template helper.

A .tef defines Event blocks that map a TUFLOW variable (~DUR~, ~AEP~, ~TP~)
to a label used in TUFLOW filename templates. Example block:

    Define Event == 24hr
        BC Event Source == ~DUR~ | 24hr
    End Define

    Define Event == 1%AEP
        BC Event Source == ~AEP~ | 1%
    End Define

    Define Event == TP_10
        BC Event Source == ~TP~ | tp10
    End Define

parse_tef() returns three lookup tables so the Results Viewer can build
export filenames using .tef labels instead of the raw RORB stems:

    {'DUR': {dur_min: label},        # e.g. {360: '6hr', 1440: '24hr'}
     'AEP': {aep_label: label},      # e.g. {'1%': '1%', '10%': '10%'}
     'TP':  {tp_num: label}}         # e.g. {1: 'tp01', 10: 'tp10'}

apply_template() substitutes ~DUR~/~AEP~/~TP~ tokens in a user-editable
template string with the .tef labels for a given event. Unknown lookups
fall back to sensible defaults so a partial .tef still produces filenames.
"""

import re

_DEFINE_RE = re.compile(r'^\s*Define\s+Event\s*==\s*(\S+)', re.IGNORECASE)
_BC_RE     = re.compile(r'^\s*BC\s+Event\s+Source\s*==\s*~(\w+)~\s*\|\s*([^!]+?)\s*(?:!.*)?$',
                        re.IGNORECASE)


def _duration_name_to_min(name):
    """'6hr' → 360, '24hour' → 1440, '90min' → 90, '1_5hr' → 90; None on failure."""
    s = name.lower().replace('_', '.')
    m = re.match(r'^\s*([\d.]+)\s*(hour|hr|h|min|m)?\s*$', s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2) or 'hr'
    if unit.startswith('h'):
        return int(round(n * 60))
    return int(round(n))


def _normalize_aep(aep):
    """'20%AEP' → '20%', ' 1 % ' → '1%', '1in100' → '1 in 100'."""
    s = aep.strip()
    s = re.sub(r'\s*AEP\s*$', '', s, flags=re.IGNORECASE).strip()
    if s.endswith('%'):
        try:
            return f'{float(s[:-1]):g}%'
        except ValueError:
            return s
    m = re.match(r'^\s*1\s*in\s*([\d,]+)\s*$', s, re.IGNORECASE)
    if m:
        return f'1 in {m.group(1).replace(",", "")}'
    return s


def _tp_name_to_num(name):
    """'TP_10' → 10, 'tp01' → 1; None on failure."""
    m = re.search(r'(\d+)$', name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def parse_tef(path):
    """Return {'DUR': {min: label}, 'AEP': {aep: label}, 'TP': {tp_num: label}}."""
    dur, aep, tp = {}, {}, {}
    current = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = _DEFINE_RE.match(line)
            if m:
                current = m.group(1)
                continue
            m = _BC_RE.match(line)
            if not m or current is None:
                continue
            token = m.group(1).upper()
            label = m.group(2).strip()
            if token == 'DUR':
                mins = _duration_name_to_min(current)
                if mins is not None:
                    dur[mins] = label
            elif token == 'AEP':
                aep[_normalize_aep(current)] = label
            elif token == 'TP':
                num = _tp_name_to_num(current)
                if num is not None:
                    tp[num] = label
    return {'DUR': dur, 'AEP': aep, 'TP': tp}


def apply_template(template, aep_label, dur_min, tp_num, tef_maps):
    """Substitute ~DUR~ / ~AEP~ / ~TP~ in template using tef_maps lookups.

    Missing lookups fall back to the raw run values so unmapped events
    still produce a sensible filename (e.g. TP not in .tef → 'tp7').
    """
    if not template:
        return ''
    dur_label = (tef_maps.get('DUR', {}).get(dur_min)
                 or _default_dur_label(dur_min))
    aep_out   = (tef_maps.get('AEP', {}).get(_normalize_aep(aep_label))
                 or aep_label.replace('%', 'pct').replace(' ', ''))
    tp_label  = (tef_maps.get('TP', {}).get(tp_num)
                 or f'tp{tp_num:02d}')
    out = template
    out = re.sub(r'~DUR~', dur_label, out, flags=re.IGNORECASE)
    out = re.sub(r'~AEP~', aep_out,   out, flags=re.IGNORECASE)
    out = re.sub(r'~TP~',  tp_label,  out, flags=re.IGNORECASE)
    return out


def _default_dur_label(dur_min):
    if dur_min is None:
        return ''
    if dur_min < 60:
        return f'{dur_min}min'
    h = dur_min / 60.0
    if h == int(h):
        return f'{int(h)}hr'
    return f'{h:g}hr'.replace('.', '_')
