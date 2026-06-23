def classFactory(iface):
    from .plugin import RorbSuitePlugin
    return RorbSuitePlugin(iface)
