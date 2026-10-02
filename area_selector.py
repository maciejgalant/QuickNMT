"""Native QgsMapCanvas AOI selector with asynchronous WMS/WFS requests.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QImage
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton,
    QSlider, QToolButton, QVBoxLayout,
)
from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsCoordinateTransformContext, QgsGeometry, QgsMessageLog, QgsPointXY,
    QgsProject, QgsRectangle,
)
from qgis.gui import QgsMapCanvas, QgsMapCanvasItem, QgsMapTool, QgsMapToolPan, QgsRubberBand

from .aoi import MAP_CRS, clean_polygon, extent_geometry, geometry_from_wkb, transform_polygon
from .capabilities import cached, remember, parse_xml
from .models import MapSelection
from .network import request_bytes, service_url
from .sheet_index import SheetLookup
from .sheet_selection import AreaSheetLookup, MAX_SELECTED_SHEETS
from .basemap import create_osm_layer


class WmsImageItem(QgsMapCanvasItem):
    """Georeferenced WMS image item; avoids synchronous provider startup.

    QgsMapCanvasItem updates its position during pan/zoom. Each image is tied
    to the exact BBOX requested; it is never used as the selected geometry.
    """
    def __init__(self, canvas):
        super().__init__(canvas)
        self.image = QImage()
        self.setZValue(1)

    def set_image(self, image, extent):
        self.image = image
        self.setRect(extent)
        self.update()

    def paint(self, painter, option=None, widget=None):
        if not self.image.isNull():
            painter.drawImage(self.boundingRect(), self.image)


class RectangleTool(QgsMapTool):
    completed = pyqtSignal(object)
    hint = pyqtSignal(str)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.start_point = None
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setColor(QColor(30, 130, 210, 65))
        self.band.setStrokeColor(QColor(30, 130, 210))
        self.band.setWidth(2)
        self.band.setZValue(40)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def reset(self):
        self.start_point = None
        self.band.reset(Qgis.GeometryType.Polygon)

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_point = QgsPointXY(event.mapPoint())

    def _rectangle(self, point):
        return QgsRectangle(self.start_point, QgsPointXY(point))

    def canvasMoveEvent(self, event):
        if self.start_point is not None:
            self.band.setToGeometry(QgsGeometry.fromRect(self._rectangle(event.mapPoint())), None)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.start_point is None:
            return
        rectangle = self._rectangle(event.mapPoint())
        self.reset()
        if rectangle.width() < self.canvas().mapUnitsPerPixel() * 2 or rectangle.height() < self.canvas().mapUnitsPerPixel() * 2:
            self.hint.emit("Przeciągnij większy prostokąt.")
            return
        self.completed.emit(QgsGeometry.fromRect(rectangle))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reset()
            event.accept()

    def deactivate(self):
        self.reset()
        super().deactivate()


class PolygonTool(QgsMapTool):
    completed = pyqtSignal(object)
    hint = pyqtSignal(str)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.points = []
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setColor(QColor(30, 130, 210, 65))
        self.band.setStrokeColor(QColor(30, 130, 210))
        self.band.setWidth(2)
        self.band.setZValue(40)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def reset(self):
        self.points = []
        self.band.reset(Qgis.GeometryType.Polygon)

    def _preview(self, moving=None):
        self.band.reset(Qgis.GeometryType.Polygon)
        points = self.points + ([QgsPointXY(moving)] if moving is not None else [])
        for point in points:
            self.band.addPoint(point, False)
        self.band.show()
        self.band.updatePosition()
        self.band.update()

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.points.append(QgsPointXY(event.mapPoint()))
            self._preview()
        elif event.button() == Qt.MouseButton.RightButton:
            self.finish()

    def canvasMoveEvent(self, event):
        if self.points:
            self._preview(event.mapPoint())

    def finish(self):
        if len(self.points) < 3:
            self.hint.emit("Poligon wymaga co najmniej trzech wierzchołków.")
            return
        geometry = QgsGeometry.fromPolygonXY([self.points + [self.points[0]]])
        self.reset()
        self.completed.emit(geometry)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            self.finish()
            event.accept()
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if self.points:
                self.points.pop()
                self._preview()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.reset()
            event.accept()

    def deactivate(self):
        self.reset()
        super().deactivate()


class SheetClickTool(RectangleTool):
    """A short click toggles one sheet; a drag adds all intersecting sheets."""
    clicked = pyqtSignal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.press_pixel = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_pixel = event.pos()
        super().canvasPressEvent(event)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.start_point is None:
            return
        if self.press_pixel is not None and (event.pos() - self.press_pixel).manhattanLength() <= 4:
            self.reset()
            self.clicked.emit(QgsPointXY(event.mapPoint()))
        else:
            super().canvasReleaseEvent(event)
        self.press_pixel = None


class AreaSelectorDialog(QDialog):
    def __init__(self, iface, family, choice, parent=None, initial=None, load_services=True):
        super().__init__(parent)
        self.iface = iface
        self.family = family
        self.choice = choice
        self.product_key = (family.key, choice.key)
        self.context = QgsCoordinateTransformContext(QgsProject.instance().transformContext())
        self.result_selection = None
        self.closed = False
        self.cap_jobs = {}
        self.map_job = None
        self.lookup = None
        self.wms = None
        self.wfs = None
        self.selected_sheets = {}
        self.drawn = None
        self.osm_layer = None
        self.load_basemap = load_services
        self.selection_mode = "rectangle"
        self.setWindowTitle("QuickNMT — wybór na mapie Geoportalu")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(980, 720)
        self.setMinimumSize(640, 500)
        self._build_ui()
        if self.load_basemap:
            self._toggle_basemap(True)
        if initial is not None and (initial.mode != "sheets" or initial.product_key == self.product_key):
            self.selection_mode = initial.mode
            if initial.mode == "sheets":
                self.selected_sheets = {record.key: record for record in initial.sheets}
            else:
                self.drawn = geometry_from_wkb(initial.geometry_wkb)
            self._refresh_selection()
        self.zoom_project(fallback=True)
        self.finished.connect(self._shutdown)
        if load_services:
            self._load_capabilities("WMS")
            self._load_capabilities("WFS")

    def _build_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel(f"{self.family.data_type}  •  {self.family.vertical_datum}  •  {self.choice.label}")
        title.setWordWrap(True)
        layout.addWidget(title)
        row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for name, label in (("pan", "Przesuwanie"), ("sheets", "Arkusze"),
                            ("rectangle", "Prostokąt"), ("polygon", "Poligon")):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            self.mode_group.addButton(button)
            self.mode_buttons[name] = button
            button.clicked.connect(lambda checked=False, mode=name: self.set_mode(mode))
            row.addWidget(button)
        row.addStretch()
        for label, callback in (("+", lambda: self.canvas.zoomIn()), ("−", lambda: self.canvas.zoomOut())):
            button = QPushButton(label)
            button.setFixedWidth(30)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        nav = QHBoxLayout()
        self.project_btn = QPushButton("Zasięg projektu QGIS")
        self.project_btn.clicked.connect(lambda: self.zoom_project())
        nav.addWidget(self.project_btn)
        self.poland_btn = QPushButton("Cała Polska")
        self.poland_btn.clicked.connect(self.zoom_poland)
        nav.addWidget(self.poland_btn)
        self.clear_btn = QPushButton("Wyczyść wybór")
        self.clear_btn.clicked.connect(self.clear_selection)
        nav.addWidget(self.clear_btn)
        nav.addStretch()
        layout.addLayout(nav)
        background = QHBoxLayout()
        self.osm_check = QCheckBox("Podkład OpenStreetMap")
        self.osm_check.setChecked(True)
        self.osm_check.toggled.connect(self._toggle_basemap)
        background.addWidget(self.osm_check)
        background.addWidget(QLabel("Widoczność skorowidzu"))
        self.overlay_opacity = QSlider(Qt.Orientation.Horizontal)
        self.overlay_opacity.setRange(0, 100)
        self.overlay_opacity.setValue(50)
        self.overlay_opacity.setToolTip("0%: sam podkład; 100%: pełne kolory skorowidzu")
        background.addWidget(self.overlay_opacity, 1)
        layout.addLayout(background)
        self.canvas = QgsMapCanvas(self)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(MAP_CRS))
        self.canvas.setCanvasColor(QColor(247, 249, 250))
        self.canvas.setMinimumSize(300, 220)
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.canvas, 1)
        self.image_item = WmsImageItem(self.canvas)
        self.image_item.setOpacity(0.5)
        self.overlay_opacity.valueChanged.connect(lambda value: self.image_item.setOpacity(value / 100))
        attribution = QLabel('© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> · Skorowidz: GUGiK / PZGiK')
        attribution.setOpenExternalLinks(True)
        attribution.setWordWrap(True)
        layout.addWidget(attribution)
        self.selection_band = QgsRubberBand(self.canvas, Qgis.GeometryType.Polygon)
        self.selection_band.setColor(QColor(40, 180, 70, 65))
        self.selection_band.setStrokeColor(QColor(0, 120, 35))
        self.selection_band.setWidth(3)
        self.selection_band.setZValue(30)
        self.tools = {"pan": QgsMapToolPan(self.canvas), "rectangle": RectangleTool(self.canvas),
                      "polygon": PolygonTool(self.canvas), "sheets": SheetClickTool(self.canvas)}
        for key in ("rectangle", "polygon"):
            self.tools[key].completed.connect(lambda geometry, mode=key: self._draw_completed(geometry, mode))
            self.tools[key].hint.connect(self._status)
        self.tools["sheets"].clicked.connect(self.toggle_sheet)
        self.tools["sheets"].completed.connect(self.select_sheets_in_area)
        self.tools["sheets"].hint.connect(self._status)
        self.mode_buttons["sheets"].setEnabled(False)
        self.mode_buttons["sheets"].setToolTip("Oczekiwanie na aktualny skorowidz WFS.")
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.count_label = QLabel("Nie wybrano obszaru.")
        layout.addWidget(self.count_label)
        self.map_status = QLabel("Wczytuję skorowidz WMS…")
        self.map_status.setTextFormat(Qt.TextFormat.PlainText)
        self.map_status.setWordWrap(True)
        layout.addWidget(self.map_status)
        self.status_label = QLabel("Wczytuję informacje o arkuszach WFS…")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        note = QLabel("Arkusze: kliknij pojedynczy lub przeciągnij prostokąt, aby dodać wiele. Wybierane są najnowsze źródła zgodne z produktem i rozdzielczością.")
        note.setWordWrap(True)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Anuluj")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self.accept_btn = QPushButton("Zatwierdź")
        self.accept_btn.setEnabled(False)
        self.accept_btn.clicked.connect(self.accept_selection)
        footer.addWidget(self.accept_btn)
        layout.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.map_timer = QTimer(self)
        self.map_timer.setSingleShot(True)
        self.map_timer.setInterval(350)
        self.map_timer.timeout.connect(self._request_map)
        self.canvas.extentsChanged.connect(self._map_extent_changed)
        self.set_mode("pan")

    def _status(self, text):
        self.status_label.setText(text)

    def _toggle_basemap(self, enabled):
        if not self.load_basemap:
            return
        try:
            if enabled and self.osm_layer is None:
                self.osm_layer = create_osm_layer()
            self.canvas.setLayers([self.osm_layer] if enabled and self.osm_layer else [])
            self.canvas.refresh()
        except Exception as exc:
            self._status(str(exc))
            QgsMessageLog.logMessage(str(exc), "QuickNMT", Qgis.MessageLevel.Warning)

    def _load_capabilities(self, kind):
        endpoint = self.family.wms if kind == "WMS" else self.family.wfs
        value = cached(endpoint, kind)
        if value:
            self._capabilities_ready(kind, value)
            return
        url = service_url(endpoint, SERVICE=kind, VERSION="1.3.0" if kind == "WMS" else "2.0.0",
                          REQUEST="GetCapabilities")
        self.cap_jobs[kind] = request_bytes(url, self,
            lambda payload: self._capabilities_received(kind, endpoint, payload),
            lambda message: self._capabilities_error(kind, message))

    def _capabilities_received(self, kind, endpoint, payload):
        self.cap_jobs.pop(kind, None)
        if self.closed:
            return
        try:
            self._capabilities_ready(kind, remember(endpoint, kind, payload))
        except Exception as exc:
            self._capabilities_error(kind, str(exc))

    def _capabilities_ready(self, kind, value):
        if kind == "WMS":
            self.wms = value
            self.map_timer.start()
        else:
            self.wfs = value
            self.mode_buttons["sheets"].setEnabled(True)
            self.mode_buttons["sheets"].setToolTip("Kliknij arkusz lub przeciągnij prostokąt, aby zaznaczyć wiele. Ponowne kliknięcie odznacza.")
            self._status("Skorowidz WFS gotowy. Możesz wybierać arkusze.")

    def _capabilities_error(self, kind, message):
        self.cap_jobs.pop(kind, None)
        QgsMessageLog.logMessage(f"{kind}: {message}", "QuickNMT", Qgis.MessageLevel.Warning)
        if kind == "WMS":
            self.map_status.setText("Nie udało się wyświetlić mapy: " + message)
        else:
            self._status("Wybór arkuszy niedostępny: " + message)

    def _map_extent_changed(self):
        if not self.closed:
            self.map_timer.start()

    def _request_map(self):
        if self.closed or self.wms is None or self.canvas.extent().isEmpty():
            return
        if self.map_job is not None:
            self.map_job.cancel()
        extent = QgsRectangle(self.canvas.extent())
        width = min(1600, max(100, self.canvas.width()))
        height = min(1200, max(100, self.canvas.height()))
        bbox = f"{extent.xMinimum()},{extent.yMinimum()},{extent.xMaximum()},{extent.yMaximum()}"
        url = service_url(self.family.wms, SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap",
                          LAYERS=",".join(self.wms.names), STYLES=",".join("" for _ in self.wms.names),
                          CRS=MAP_CRS, BBOX=bbox, WIDTH=width, HEIGHT=height,
                          FORMAT="image/png", TRANSPARENT="TRUE")
        self.map_status.setText("Odświeżam mapę Geoportalu…")
        self.map_job = request_bytes(url, self, lambda payload: self._map_received(payload, extent), self._map_error)

    def _map_received(self, payload, extent):
        self.map_job = None
        if self.closed:
            return
        image = QImage.fromData(payload)
        if image.isNull():
            try:
                parse_xml(payload)
                raise ValueError("WMS nie zwrócił obrazu mapy.")
            except Exception as exc:
                self._map_error(str(exc))
            return
        self.image_item.set_image(image, extent)
        self.map_status.setText("Mapa: oficjalny skorowidz Geoportalu / PZGiK. Kółko myszy przybliża i oddala.")

    def _map_error(self, message):
        self.map_job = None
        self.map_status.setText("Nie udało się odświeżyć mapy: " + message)
        QgsMessageLog.logMessage(f"WMS: {message}", "QuickNMT", Qgis.MessageLevel.Warning)

    def set_mode(self, mode):
        self._cancel_lookup()
        self.mode_buttons[mode].setChecked(True)
        self.canvas.setMapTool(self.tools[mode])
        hints = {"pan": "Przeciągnij mapę. Przybliż interesujące miejsce przed wyborem arkuszy.",
                 "sheets": "Kliknij arkusz lub przeciągnij prostokąt, aby dodać wszystkie przecinające go arkusze. Ponowne kliknięcie odznacza.",
                 "rectangle": "Przytrzymaj lewy przycisk myszy i przeciągnij prostokąt.",
                 "polygon": "Klikaj wierzchołki. Enter lub prawy przycisk kończy poligon; Backspace cofa wierzchołek."}
        self.hint.setText(hints[mode])
        self.canvas.setFocus()

    def zoom_poland(self):
        transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                            QgsCoordinateReferenceSystem(MAP_CRS), self.context)
        extent = transform.transformBoundingBox(QgsRectangle(14.0, 48.9, 24.3, 55.0))
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def zoom_project(self, fallback=False):
        try:
            source_canvas = self.iface.mapCanvas()
            geometry, _ = transform_polygon(extent_geometry(source_canvas.extent()),
                source_canvas.mapSettings().destinationCrs().toWkt(), MAP_CRS, self.context)
            self.canvas.setExtent(geometry.boundingBox())
            self.canvas.refresh()
        except Exception as exc:
            if fallback:
                self.zoom_poland()
            else:
                self._status("Nie udało się odczytać zasięgu projektu: " + str(exc))
            QgsMessageLog.logMessage(str(exc), "QuickNMT", Qgis.MessageLevel.Warning)

    def _draw_completed(self, geometry, mode):
        try:
            geometry, repaired = clean_polygon(geometry)
            self._cancel_lookup()
            self.selected_sheets.clear()
            self.drawn = geometry
            self.selection_mode = mode
            self._refresh_selection()
            self._status("Obszar narysowany." + (" Naprawiono przecięcie geometrii." if repaired else ""))
        except ValueError as exc:
            self._status(str(exc))

    def _cancel_lookup(self):
        if self.lookup is not None:
            self.lookup.cancel()
            self.lookup.deleteLater()
            self.lookup = None
        if hasattr(self, "accept_btn"):
            self.accept_btn.setEnabled(bool(self.selected_sheets) or self.drawn is not None)

    def toggle_sheet(self, point):
        if self.wfs is None or self.lookup is not None:
            return
        point_geometry = QgsGeometry.fromPointXY(point)
        for key, record in list(self.selected_sheets.items()):
            if geometry_from_wkb(record.geometry_wkb).intersects(point_geometry):
                del self.selected_sheets[key]
                self._refresh_selection()
                self._status("Arkusz odznaczony.")
                return
        if self.canvas.scale() > 500000:
            self._status("Przybliż mapę do skali 1:500 000 lub większej, aby wskazać konkretny arkusz.")
            return
        tolerance = max(0.5, min(2000.0, self.canvas.mapUnitsPerPixel() * 3))
        bbox = QgsRectangle(point.x() - tolerance, point.y() - tolerance,
                            point.x() + tolerance, point.y() + tolerance)
        bbox = bbox.intersect(self.canvas.extent())
        self.lookup = SheetLookup(self.family, self.choice, self.wfs.names, bbox, point, self)
        self.lookup.ready.connect(self._sheet_ready)
        self.lookup.failed.connect(self._sheet_error)
        self.lookup.status.connect(self._status)
        self.accept_btn.setEnabled(False)
        self.lookup.start()

    def select_sheets_in_area(self, geometry):
        if self.wfs is None:
            self._status("Poczekaj na wczytanie skorowidzu WFS.")
            return
        self._cancel_lookup()
        if self.canvas.scale() > 500000:
            self._status("Przybliż mapę do skali 1:500 000 lub większej, aby zaznaczać arkusze.")
            return
        self.lookup = AreaSheetLookup(self.family, self.choice, geometry, self, self.wfs.names)
        self.lookup.ready.connect(self._sheets_ready)
        self.lookup.failed.connect(self._sheet_error)
        self.lookup.status.connect(self._status)
        self.accept_btn.setEnabled(False)
        self.lookup.start()

    def _sheets_ready(self, records):
        finished, self.lookup = self.lookup, None
        if finished is not None:
            finished.deleteLater()
        if self.closed:
            return
        updated = dict(self.selected_sheets)
        for record in records:
            previous = updated.get(record.key)
            if previous is None or record.year >= previous.year:
                updated[record.key] = record
        if len(updated) > MAX_SELECTED_SHEETS:
            self._status("Limit wynosi 500 zaznaczonych arkuszy. Wyczyść wybór lub zaznacz mniejszy obszar.")
        elif records:
            added = len(updated) - len(self.selected_sheets)
            self.drawn = None
            self.selection_mode = "sheets"
            self.selected_sheets = updated
            self._status(f"Dodano arkuszy: {added}. Wybrano łącznie: {len(updated)}.")
        else:
            self._status("W prostokącie nie znaleziono arkuszy zgodnych z wybranym produktem.")
        self._refresh_selection()

    def _sheet_ready(self, record):
        finished = self.lookup
        self.lookup = None
        if finished is not None:
            finished.deleteLater()
        if self.closed:
            return
        if record is None:
            self._status("W tym miejscu nie znaleziono arkusza zgodnego z wybranym produktem i rozdzielczością.")
        else:
            if record.key not in self.selected_sheets and len(self.selected_sheets) >= MAX_SELECTED_SHEETS:
                self._status("Limit wynosi 500 zaznaczonych arkuszy. Wyczyść część wyboru.")
                self._refresh_selection()
                return
            self.drawn = None
            self.selection_mode = "sheets"
            self.selected_sheets[record.key] = record
            self._status(f"Wybrano {record.sheet_id}; rok {record.year}; źródłowe {record.resolution:g} m.")
        self._refresh_selection()

    def _sheet_error(self, message):
        finished = self.lookup
        self.lookup = None
        if finished is not None:
            finished.deleteLater()
        self._status(message)
        self._refresh_selection()

    def selection_geometry(self):
        if self.selected_sheets:
            geometry = QgsGeometry.unaryUnion([geometry_from_wkb(record.geometry_wkb)
                                               for record in self.selected_sheets.values()])
            return clean_polygon(geometry)[0]
        return QgsGeometry(self.drawn) if self.drawn is not None else None

    def _refresh_selection(self):
        geometry = self.selection_geometry()
        self.selection_band.reset(Qgis.GeometryType.Polygon)
        if geometry is not None:
            self.selection_band.setToGeometry(geometry, None)
        self.count_label.setText(f"Wybrano arkuszy: {len(self.selected_sheets)}" if self.selected_sheets
                                else ("Obszar: narysowany " + ("prostokąt" if self.selection_mode == "rectangle" else "poligon")
                                      if self.drawn is not None else "Nie wybrano obszaru."))
        self.accept_btn.setEnabled(geometry is not None and self.lookup is None)

    def clear_selection(self):
        self._cancel_lookup()
        self.selected_sheets.clear()
        self.drawn = None
        for key in ("rectangle", "polygon", "sheets"):
            self.tools[key].reset()
        self._refresh_selection()
        self._status("Wybór wyczyszczony.")

    def accept_selection(self):
        if self.lookup is not None:
            return
        geometry = self.selection_geometry()
        if geometry is None:
            self._status("Najpierw wybierz lub narysuj obszar.")
            return
        self.result_selection = MapSelection(bytes(geometry.asWkb()), MAP_CRS, self.selection_mode,
                                             self.product_key, tuple(self.selected_sheets.values()))
        self.accept()

    def _shutdown(self):
        self.closed = True
        self.map_timer.stop()
        self._cancel_lookup()
        for job in list(self.cap_jobs.values()):
            job.cancel()
        self.cap_jobs.clear()
        if self.map_job is not None:
            self.map_job.cancel()
            self.map_job = None
        self.canvas.unsetMapTool(self.canvas.mapTool())
        self.canvas.stopRendering()
        self.canvas.setLayers([])

    def keyPressEvent(self, event):
        # Return ends the polygon rather than activating a dialog button.
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and self.canvas.mapTool() is self.tools["polygon"]):
            self.tools["polygon"].finish()
            event.accept()
            return
        super().keyPressEvent(event)
