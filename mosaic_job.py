"""GUI-thread coordinator; all source validation and raster work run in tasks.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from pathlib import Path
import math
import tempfile
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import QgsApplication, QgsMessageLog, Qgis
from .aoi import geometry_from_wkb, transform_polygon
from .download_manager import SourceDownload, cleanup_directory
from .sheet_selection import AreaSheetLookup
from .mosaic import MosaicTask, grid_plan, validate_source_products
from .export_formats import TEXT_FORMATS, BIG_TEXT_CELLS, ESTIMATED_BYTES_PER_POINT


def text_export_estimate(aoi, keep_buffer, plan, context):
    geometry, _ = transform_polygon(geometry_from_wkb(aoi.buffered_wkb if keep_buffer else aoi.original_wkb),
                                    aoi.crs, plan['crs_wkt'], context)
    rectangle = geometry.boundingBox()
    # Upper estimate before rasterisation, including possible edge alignment.
    width = math.ceil(rectangle.width() / plan['resolution']) + 2
    height = math.ceil(rectangle.height() / plan['resolution']) + 2
    cells = width * height
    return {'cells': cells, 'estimated_bytes': cells * ESTIMATED_BYTES_PER_POINT,
            'requires_confirmation': cells >= BIG_TEXT_CELLS}


class MosaicJob(QObject):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    status = pyqtSignal(str)
    progress = pyqtSignal(int)
    confirmation_required = pyqtSignal(object)
    export_confirmation_required = pyqtSignal(object)

    def __init__(self, aoi, family, choice, target, keep_buffer, context, snapshot, parent=None,
                 output_format='GeoTIFF'):
        super().__init__(parent)
        self.aoi, self.family, self.choice = aoi, family, choice
        self.target, self.keep_buffer, self.context = target, keep_buffer, context
        self.snapshot = snapshot
        self.output_format = output_format
        self.resampling_approved = False
        self.output_resolution = None
        self.awaiting_export_confirmation = False
        self.lookup = self.download = self.task = self.workspace = None
        self.records, self.sources = [], []
        self.stopped = self.cancel_requested = self.awaiting_confirmation = False

    def start(self):
        try:
            # Large jobs may contain many source rasters. Keep the temporary workspace
            # on the same drive as the requested result instead of filling system TEMP.
            base = Path(self.target).expanduser().resolve().parent
            if not base.is_dir():
                raise ValueError('Folder docelowy wyniku nie istnieje.')
            self.workspace = Path(tempfile.mkdtemp(prefix='.QuickNMT-mosaic-', dir=str(base)))
            geometry, _ = transform_polygon(geometry_from_wkb(self.aoi.buffered_wkb),
                                            self.aoi.crs, 'EPSG:3857', self.context)
            # Also search neighbours around manually selected sheets, even if the
            # technical buffer will be removed from the final raster.
            self.lookup = AreaSheetLookup(self.family, self.choice, geometry, self)
            self.lookup.ready.connect(self._found)
            self.lookup.failed.connect(self._failed)
            self.lookup.status.connect(self.status)
            self.progress.emit(0)
            self.lookup.start()
        except Exception as exc:
            self._failed(str(exc))

    def _found(self, records):
        if self.stopped:
            return
        if self.lookup is not None:
            self.lookup.deleteLater()
            self.lookup = None
        if not records:
            self._failed('Nie znaleziono źródeł wybranego produktu dla tego obszaru.')
            return
        self.records = list(records)
        self._next()

    def _next(self):
        if self.stopped or self.cancel_requested:
            return
        try:
            index = len(self.sources)
            if index == len(self.records):
                validate_source_products(self.sources, self.family, self.choice)
                plan = grid_plan(self.sources)
                if plan['resampled']:
                    self.awaiting_confirmation = True
                    self.status.emit('Arkusze wymagają wyrównania siatki — czekam na decyzję.')
                    self.confirmation_required.emit(plan)
                else:
                    self.continue_mosaic(False)
                return
            self.download = SourceDownload(self.records[index], self.family, self.choice,
                                            self.workspace, self.context, QgsApplication.instance())
            self.download.ready.connect(self._source_ready)
            self.download.failed.connect(self._failed)
            self.download.cancelled.connect(self._cancelled)
            self.download.status.connect(lambda message: self.status.emit(
                f'Arkusz {index + 1}/{len(self.records)}: {message}'))
            self.download.progress.connect(lambda value: self.progress.emit(
                5 + round(65 * (index + value / 100) / len(self.records))))
            self.download.start()
        except Exception as exc:
            self._failed(str(exc))

    def _source_ready(self, result):
        self.download = None
        if self.cancel_requested:
            self._cancelled()
            return
        result['record'] = self.records[len(self.sources)]
        self.sources.append(result)
        QTimer.singleShot(0, self._next)

    def continue_mosaic(self, allow_resampling, output_resolution=None):
        if self.stopped or self.cancel_requested:
            return
        try:
            plan = grid_plan(self.sources, output_resolution)
            if plan['resolution_choice_required'] or (plan['resampled'] and not allow_resampling):
                raise ValueError('Wybierz i zatwierdź wspólną siatkę wyniku przed połączeniem.')
            self.awaiting_confirmation = False
            self.resampling_approved = allow_resampling
            self.output_resolution = output_resolution
            if self.output_format in TEXT_FORMATS:
                estimate = text_export_estimate(self.aoi, self.keep_buffer, plan, self.context)
                if estimate['requires_confirmation']:
                    self.awaiting_export_confirmation = True
                    self.status.emit('Duży eksport tekstowy — czekam na decyzję.')
                    self.export_confirmation_required.emit(estimate)
                    return
            self._start_mosaic_task()
        except Exception as exc:
            self._failed(str(exc))

    def confirm_export(self, accepted):
        if self.stopped or self.cancel_requested or not self.awaiting_export_confirmation:
            return
        self.awaiting_export_confirmation = False
        if accepted:
            self._start_mosaic_task()
        else:
            self.cancel()

    def _start_mosaic_task(self):
        try:
            self.status.emit(f'Łączę {len(self.sources)} arkuszy i przycinam wynik do obszaru…')
            self.task = MosaicTask(sources=self.sources, aoi=self.aoi, keep_buffer=self.keep_buffer,
                family=self.family, choice=self.choice, target=self.target, context=self.context,
                workspace=self.workspace, snapshot=self.snapshot, allow_resampling=self.resampling_approved,
                output_format=self.output_format, output_resolution=self.output_resolution)
            self.task.ready.connect(self._ready)
            self.task.failed.connect(self._failed)
            self.task.cancelled.connect(self._cancelled)
            self.task.progressChanged.connect(lambda value: self.progress.emit(70 + round(value * .3)))
            self.task.status_message.connect(self.status)
            QgsApplication.taskManager().addTask(self.task)
        except Exception as exc:
            self._failed(str(exc))

    def _cleanup(self):
        self.stopped = True
        self.task = self.download = None
        if self.lookup is not None:
            self.lookup.cancel()
            self.lookup.deleteLater()
            self.lookup = None
        cleanup_directory(self.workspace)

    def _ready(self, result):
        self._cleanup()
        self.progress.emit(100)
        self.ready.emit(result)
        self.deleteLater()

    def _failed(self, message):
        if self.stopped:
            return
        if self.cancel_requested:
            self._cancelled()
            return
        self._cleanup()
        QgsMessageLog.logMessage(message, 'QuickNMT', Qgis.MessageLevel.Warning)
        self.failed.emit(message)
        self.deleteLater()

    def cancel(self):
        if self.stopped or self.cancel_requested:
            return
        self.cancel_requested = True
        if self.download is not None:
            self.download.cancel()
        elif self.task is not None:
            self.task.cancel()
        else:
            self._cancelled()

    def _cancelled(self):
        if self.stopped:
            return
        self._cleanup()
        self.cancelled.emit()
        self.deleteLater()
