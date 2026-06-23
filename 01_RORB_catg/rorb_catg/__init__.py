# -*- coding: utf-8 -*-
"""
/***************************************************************************
 RORB Catchment Tools
                                 A QGIS plugin
 Prepare GIS layers and build RORB/URBS control vectors from a catchment.
 ***************************************************************************/
"""

__author__ = 'Tom Norman'
__date__ = '2023-06-15'
__copyright__ = '(C) 2025 by Tom Norman'

import os
import sys

try:
    import pyromb  # noqa: F401  (use the user's own install if present)
except ImportError:
    _vendor_dir = os.path.join(os.path.dirname(__file__), 'vendor')
    if _vendor_dir not in sys.path:
        sys.path.insert(0, _vendor_dir)


def classFactory(iface):
    from .rorb_catg import RorbCatgPlugin
    return RorbCatgPlugin(iface)
