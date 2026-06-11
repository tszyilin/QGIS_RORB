def classFactory(iface):
    from .plugin import RorbQgisPlugin
    return RorbQgisPlugin(iface)
