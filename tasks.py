"""Background AOI preparation from value copies / feature-source snapshots.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import traceback
from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import Qgis, QgsTask, QgsMessageLog
from .aoi import build_aoi, feature_geometries, geometry_from_wkb, CancelledError


class AoiTask(QgsTask):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, source_crs, context, buffer_m, mode, geometry_wkb=None,
                 feature_source=None, selected_ids=None):
        super().__init__("QuickNMT — przygotowanie obszaru", QgsTask.Flag.CanCancel)
        self.source_crs = source_crs
        self.context = context
        self.buffer_m = buffer_m
        self.mode = mode
        self.geometry_wkb = geometry_wkb
        self.feature_source = feature_source
        self.selected_ids = selected_ids
        self.result = None
        self.error = ""
        self.details = ""

    def run(self):
        try:
            geometries = (feature_geometries(self.feature_source, self.selected_ids)
                          if self.feature_source is not None
                          else [geometry_from_wkb(self.geometry_wkb)])
            self.result = build_aoi(geometries, self.source_crs, self.context, self.buffer_m,
                                    self.mode, self.isCanceled, self.setProgress)
            return not self.isCanceled()
        except CancelledError:
            return False
        except Exception as exc:
            self.error = str(exc)
            self.details = traceback.format_exc()
            return False

    def finished(self, success):
        if self.isCanceled():
            self.cancelled.emit()
        elif success:
            self.ready.emit(self.result)
        else:
            QgsMessageLog.logMessage(self.details, "QuickNMT", Qgis.MessageLevel.Warning)
            self.failed.emit(self.error or "Nie udało się przygotować obszaru.")
