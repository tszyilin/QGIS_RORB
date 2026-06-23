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


def classFactory(iface):
    from .rorb_catg import RorbCatgPlugin
    return RorbCatgPlugin(iface)
