"""Bounded WFS area queries and selection of the newest source sheets.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import hashlib
import traceback
import math
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsApplication, QgsGeometry, QgsRectangle, QgsTask, QgsMessageLog, Qgis
from .aoi import clean_polygon, geometry_from_wkb, CancelledError
from .capabilities import cached, remember, year_from_name
from .network import request_bytes, service_url
from .sheet_index import parse_features, WFS_CRS, PAGE_SIZE

MAX_RAW_RECORDS = 100000
MAX_SELECTED_SHEETS = 2500
# Large AOIs are queried in smaller WFS BBOXes. 60 km keeps each request bounded
# while still avoiding hundreds of tiny requests for ordinary county-sized jobs.
MAX_WFS_TILE_SIDE_M = 60000.0
MAX_WFS_TILES = 400
MAX_PAGES_PER_TILE_LAYER = 50
WFS_RESPONSE_MAX_BYTES = 32 * 1024 * 1024


def newest_sheets(records, aoi, cancelled=lambda: False):
    """Retain newest versions and older sheets only where they add coverage.

    Partial overlaps remain for the later mosaic stage; no raster is resampled.
    A changed sheet identifier does not cause a fully covered old copy to survive.
    """
    chosen, identifiers, urls = [], set(), set()
    covered = QgsGeometry()
    for record in sorted(records, key=lambda r: (-r.year, r.resolution, r.key, r.download_url)):
        if cancelled():
            raise CancelledError()
        geometry = geometry_from_wkb(record.geometry_wkb)
        if not geometry.intersects(aoi):
            continue
        intersection = geometry.intersection(aoi)
        if intersection.isNull() and intersection.lastError():
            raise ValueError("Nie udało się przeciąć arkusza z obszarem: " + intersection.lastError())
        if intersection.isEmpty() or intersection.area() <= 0:
            continue
        if (record.sheet_id and record.sheet_id in identifiers) or record.download_url in urls:
            continue
        if not covered.isNull():
            remainder = intersection.difference(covered)
            if remainder.isNull() and remainder.lastError():
                raise ValueError("Nie udało się porównać pokrycia arkuszy.")
            if remainder.isEmpty() or remainder.area() <= max(0.001, intersection.area() * 1e-9):
                continue
        chosen.append(record)
        identifiers.add(record.sheet_id)
        urls.add(record.download_url)
        covered = intersection if covered.isNull() else covered.combine(intersection)
        if covered.isNull():
            raise ValueError("Nie udało się połączyć pokrycia skorowidzu.")
        if len(chosen) > MAX_SELECTED_SHEETS:
            raise ValueError("Obszar obejmuje ponad 2500 arkuszy źródłowych. Podziel zadanie na kilka osobnych wyników.")
    return chosen


class RankSheetsTask(QgsTask):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, records, aoi_wkb):
        super().__init__("QuickNMT — wybór najnowszych arkuszy", QgsTask.Flag.CanCancel)
        self.records, self.aoi_wkb = records, aoi_wkb
        self.result, self.error = None, ""

    def run(self):
        try:
            self.result = newest_sheets(self.records, geometry_from_wkb(self.aoi_wkb), self.isCanceled)
            return not self.isCanceled()
        except CancelledError:
            return False
        except Exception as exc:
            self.error = str(exc)
            QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Warning)
            return False

    def finished(self, success):
        if self.isCanceled():
            return
        if success:
            self.ready.emit(self.result)
        else:
            self.failed.emit(self.error or "Nie udało się wybrać arkuszy.")


class AreaSheetLookup(QObject):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, family, choice, geometry, parent=None, type_names=None):
        super().__init__(parent)
        self.family, self.choice = family, choice
        self.aoi, _ = clean_polygon(geometry)
        self.layers = list(type_names) if type_names else None
        self.job = self.task = None
        self.cancelled = False
        self.layer_index = self.tile_index = self.page = self.raw_count = 0
        self.records, self.record_keys, self.signatures = [], set(), set()
        self.tiles = []

    def _build_tiles(self):
        """Split the AOI bounding box into bounded Web-Mercator WFS requests."""
        rect = self.aoi.boundingBox()
        if rect.isEmpty() or rect.width() <= 0 or rect.height() <= 0:
            raise ValueError("Obszar wyszukiwania arkuszy jest pusty.")
        nx = max(1, int(math.ceil(rect.width() / MAX_WFS_TILE_SIDE_M)))
        ny = max(1, int(math.ceil(rect.height() / MAX_WFS_TILE_SIDE_M)))
        if nx * ny > MAX_WFS_TILES:
            raise ValueError(
                f"Obszar wymagałby ponad {MAX_WFS_TILES} zapytań przestrzennych. "
                "Podziel eksport na kilka osobnych zadań.")
        dx, dy = rect.width() / nx, rect.height() / ny
        tiles = []
        for iy in range(ny):
            y0 = rect.yMinimum() + iy * dy
            y1 = rect.yMaximum() if iy == ny - 1 else rect.yMinimum() + (iy + 1) * dy
            for ix in range(nx):
                x0 = rect.xMinimum() + ix * dx
                x1 = rect.xMaximum() if ix == nx - 1 else rect.xMinimum() + (ix + 1) * dx
                tile = QgsRectangle(x0, y0, x1, y1)
                if QgsGeometry.fromRect(tile).intersects(self.aoi):
                    tiles.append(tile)
        if not tiles:
            raise ValueError("Nie udało się podzielić obszaru na fragmenty wyszukiwania.")
        return tiles

    def start(self):
        try:
            self.tiles = self._build_tiles()
        except Exception as exc:
            self._fail(str(exc))
            return
        if self.layers:
            self._begin()
            return
        caps = cached(self.family.wfs, "WFS")
        if caps:
            self.layers = list(caps.names)
            self._begin()
            return
        self.status.emit("Odczytuję aktualne warstwy WFS…")
        self.job = request_bytes(service_url(self.family.wfs, SERVICE="WFS", VERSION="2.0.0", REQUEST="GetCapabilities"),
                                 self, self._capabilities, self._fail, max_bytes=WFS_RESPONSE_MAX_BYTES)

    def _capabilities(self, payload):
        self.job = None
        if self.cancelled:
            return
        try:
            self.layers = list(remember(self.family.wfs, "WFS", payload).names)
            self._begin()
        except Exception as exc:
            self._fail(str(exc))

    def _begin(self):
        self.layers.sort(key=lambda name: (year_from_name(name), name), reverse=True)
        if len(self.tiles) > 1:
            self.status.emit(f"Duży obszar: dzielę wyszukiwanie na {len(self.tiles)} fragmentów…")
        self._request()

    def _advance_scope(self):
        """Advance tile first, then year layer. Return False when all work is done."""
        self.page = 0
        self.signatures.clear()
        self.tile_index += 1
        if self.tile_index < len(self.tiles):
            return True
        self.tile_index = 0
        self.layer_index += 1
        return self.layer_index < len(self.layers)

    def _request(self):
        if self.cancelled:
            return
        if self.layer_index >= len(self.layers):
            self.status.emit("Porównuję roczniki i usuwam powielone arkusze…")
            self.task = RankSheetsTask(self.records, bytes(self.aoi.asWkb()))
            self.task.ready.connect(self._ranked)
            self.task.failed.connect(self._fail)
            QgsApplication.taskManager().addTask(self.task)
            return
        if not self.tiles:
            self._fail("Brak fragmentów obszaru do przeszukania.")
            return
        self.status.emit(
            f"Szukam arkuszy: rocznik {self.layer_index + 1}/{len(self.layers)}, "
            f"fragment {self.tile_index + 1}/{len(self.tiles)}, strona {self.page + 1}…")
        rect = self.tiles[self.tile_index]
        bbox = f"{rect.xMinimum()},{rect.yMinimum()},{rect.xMaximum()},{rect.yMaximum()},{WFS_CRS}"
        url = service_url(self.family.wfs, SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature",
                          TYPENAMES=self.layers[self.layer_index], SRSNAME=WFS_CRS, BBOX=bbox,
                          COUNT=PAGE_SIZE, STARTINDEX=self.page * PAGE_SIZE,
                          OUTPUTFORMAT="application/gml+xml; version=3.2")
        self.job = request_bytes(url, self, self._received, self._fail,
                                 max_bytes=WFS_RESPONSE_MAX_BYTES)

    def _received(self, payload):
        self.job = None
        if self.cancelled:
            return
        try:
            signature = hashlib.sha256(payload).digest()
            if signature in self.signatures:
                raise ValueError("Usługa powtarza stronę WFS; wyszukiwanie przerwane.")
            self.signatures.add(signature)
            records, count, more = parse_features(payload, self.family, self.choice, self.layers[self.layer_index])
            self.raw_count += count
            if self.raw_count > MAX_RAW_RECORDS:
                raise ValueError("Skorowidz zwrócił ponad 100 000 rekordów. Podziel eksport na kilka zadań.")
            # Adjacent BBOX tiles can return the same sheet. Keep one copy before ranking.
            for record in records:
                key = (record.source_layer, record.sheet_id, record.year, record.download_url)
                if key not in self.record_keys:
                    self.record_keys.add(key)
                    self.records.append(record)
            if more or count >= PAGE_SIZE:
                self.page += 1
                if self.page >= MAX_PAGES_PER_TILE_LAYER:
                    raise ValueError(
                        "Jeden fragment skorowidzu przekroczył limit 50 stron WFS. "
                        "Spróbuj ponownie lub podziel eksport na mniejsze zadania.")
            else:
                self._advance_scope()
            self._request()
        except Exception as exc:
            self._fail(str(exc))

    def _ranked(self, records):
        self.task = None
        if not self.cancelled:
            self.ready.emit(records)

    def _fail(self, message):
        self.job = self.task = None
        if not self.cancelled:
            QgsMessageLog.logMessage(f"{message}\nWFS: {self.family.wfs}", "QuickNMT", Qgis.MessageLevel.Warning)
            self.failed.emit(message)

    def cancel(self):
        self.cancelled = True
        if self.job is not None:
            self.job.cancel()
            self.job = None
        if self.task is not None:
            self.task.cancel()
            self.task = None
