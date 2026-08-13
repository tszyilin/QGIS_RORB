import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from viewer import RorbResultsViewer


def main():
    # Enable high-DPI scaling
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("RORB Results Viewer")
    app.setOrganizationName("RORB")

    win = RorbResultsViewer()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
