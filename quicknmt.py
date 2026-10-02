"""QGIS plugin lifecycle. SPDX-License-Identifier: GPL-3.0-or-later"""
from pathlib import Path
import traceback

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import Qgis, QgsMessageLog

from .quicknmt_dialog import QuickNMTDialog


class QuickNMT:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        if self.action is not None:
            return
        icon = QIcon(str(Path(__file__).parent / "icons" / "quicknmt_toolbar.png"))
        self.action = QAction(icon, "QuickNMT", self.iface.mainWindow())
        self.action.setObjectName("QuickNMTAction")
        self.action.setToolTip("QuickNMT — NMT i NMPT z PZGiK")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToRasterMenu("&QuickNMT", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        if self.action is not None:
            self.iface.removePluginRasterMenu("&QuickNMT", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.triggered.disconnect(self.run)
            self.action.deleteLater()
            self.action = None

    def run(self):
        try:
            if self.dialog is None:
                self.dialog = QuickNMTDialog(self.iface, self.iface.mainWindow())
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()
        except Exception:
            QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Critical)
            QMessageBox.critical(self.iface.mainWindow(), "QuickNMT",
                                 "Nie udało się otworzyć QuickNMT. Szczegóły: Dziennik komunikatów → QuickNMT.")
