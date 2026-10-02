"""Bounded, asynchronous point identification against live WFS year layers.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import math
import re
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import Qgis, QgsGeometry, QgsOgcUtils, QgsPointXY, QgsMessageLog

from .aoi import clean_polygon, geometry_from_wkb
from .capabilities import parse_xml, local_name, year_from_name, xml_to_string
from .models import SheetRecord
from .network import request_bytes, service_url, validate_url

WFS_CRS = "urn:ogc:def:crs:EPSG::3857"  # East/north order, including strict GML 3.2.
PAGE_SIZE = 100
MAX_PAGES_PER_LAYER = 10


def matches_product(attributes, family, choice):
    if attributes.get("asortyment", "").strip().upper() != family.data_type:
        return False
    if attributes.get("uklad_h", "").strip() != family.vertical_datum:
        return False
    format_name = re.sub(r"[^A-Z0-9]", "", attributes.get("format", "").upper())
    if format_name not in ("ARCINFOASCIIGRID", "GEOTIFF", "TIFF", "TIF"):
        return False
    match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*m\s*", attributes.get("char_przestrz", ""))
    if not match:
        return False
    resolution = float(match.group(1).replace(",", "."))
    if not math.isfinite(resolution) or resolution <= 0:
        return False
    if choice.key == "grid_le_1m":
        return resolution <= 1.0
    if choice.key == "source_auto":
        return resolution in (0.5, 1.0)
    return abs(resolution - choice.source_resolution_m) < 1e-8


def parse_features(payload, family, choice, layer_name):
    root = parse_xml(payload)
    if local_name(root.tag) != "FeatureCollection":
        raise ValueError("Geoportal nie zwrócił kolekcji arkuszy WFS.")
    records = []
    raw_count = 0
    for member in root:
        if local_name(member.tag) != "member":
            continue
        raw_count += 1
        feature = next(iter(member), None)
        if feature is None:
            continue
        attributes = {local_name(child.tag): (child.text or "").strip() for child in feature
                      if local_name(child.tag) not in ("boundedBy", "msGeometry")}
        required = {"asortyment", "uklad_h", "format", "char_przestrz"}
        if not required.issubset(attributes):
            raise ValueError("Zmieniono pola skorowidzu WFS; nie można potwierdzić produktu arkusza.")
        if not matches_product(attributes, family, choice):
            continue
        property_element = next((e for e in feature if local_name(e.tag) == "msGeometry"), None)
        if property_element is None or not len(property_element):
            raise ValueError("Arkusz nie zawiera geometrii w skorowidzu.")
        element = property_element[0]
        srs_values = {e.get("srsName") for e in element.iter() if e.get("srsName")}
        if not srs_values or not srs_values.issubset({WFS_CRS, "EPSG:3857", "http://www.opengis.net/def/crs/EPSG/0/3857"}):
            raise ValueError("WFS zwrócił inny CRS niż żądany EPSG:3857; wybór przerwany.")
        # QgsOgcUtils accepts the GML geometry syntax but expects the classic
        # GML namespace. Serialize the already validated subtree and normalize
        # GML 3.2 to the classic namespace without reparsing untrusted XML.
        geometry = QgsOgcUtils.geometryFromGML(xml_to_string(element, normalize_gml=True))
        geometry, _ = clean_polygon(geometry)
        url = validate_url(attributes.get("url_do_pobrania", ""))
        try:
            year = int(attributes.get("akt_rok", ""))
        except ValueError:
            year = year_from_name(layer_name)
        res = float(re.search(r"\d+(?:[.,]\d+)?", attributes["char_przestrz"]).group().replace(",", "."))
        records.append(SheetRecord(attributes.get("godlo", ""), year, bytes(geometry.asWkb()),
                                   res, url, layer_name, attributes))
    return records, raw_count, bool(root.get("next"))


class SheetLookup(QObject):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, family, choice, type_names, bbox, point, parent=None):
        super().__init__(parent)
        self.family = family
        self.choice = choice
        self.layers = sorted(type_names, key=lambda name: (year_from_name(name), name), reverse=True)
        self.bbox = bbox
        self.point = QgsGeometry.fromPointXY(QgsPointXY(point))
        self.layer_index = 0
        self.page = 0
        self.candidates = []
        self.job = None
        self.cancelled = False
        self.page_signatures = set()

    def start(self):
        if not self.layers:
            self._fail("Brak warstw WFS w aktualnym skorowidzu.")
            return
        self._request_page()

    def _request_page(self):
        if self.cancelled:
            return
        if self.layer_index >= len(self.layers):
            self.ready.emit(None)
            return
        name = self.layers[self.layer_index]
        self.status.emit(f"Sprawdzam arkusz: warstwa {self.layer_index + 1}/{len(self.layers)}…")
        rect = self.bbox
        bbox = f"{rect.xMinimum()},{rect.yMinimum()},{rect.xMaximum()},{rect.yMaximum()},{WFS_CRS}"
        url = service_url(self.family.wfs, SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature",
                          TYPENAMES=name, SRSNAME=WFS_CRS, BBOX=bbox, COUNT=PAGE_SIZE,
                          STARTINDEX=self.page * PAGE_SIZE, OUTPUTFORMAT="application/gml+xml; version=3.2")
        self.job = request_bytes(url, self, self._received, self._fail)

    def _received(self, payload):
        self.job = None
        if self.cancelled:
            return
        try:
            records, count, more = parse_features(payload, self.family, self.choice, self.layers[self.layer_index])
            # A server which ignores STARTINDEX must not trigger an infinite loop.
            signature = hash(payload)
            if signature in self.page_signatures:
                raise ValueError("Usługa powtarza stronę WFS. Przybliż mapę i spróbuj ponownie.")
            self.page_signatures.add(signature)
            for record in records:
                geometry = geometry_from_wkb(record.geometry_wkb)
                if geometry.intersects(self.point):
                    self.candidates.append(record)
            if more or count >= PAGE_SIZE:
                self.page += 1
                if self.page >= MAX_PAGES_PER_LAYER:
                    raise ValueError("Zbyt wiele arkuszy w zapytaniu. Przybliż mapę.")
                self._request_page()
                return
            if self.candidates:
                # Newest year is queried first; ties prefer the smallest footprint.
                self.candidates.sort(key=lambda rec: (-rec.year, geometry_from_wkb(rec.geometry_wkb).area(), rec.key))
                self.ready.emit(self.candidates[0])
                return
            self.layer_index += 1
            self.page = 0
            self.page_signatures.clear()
            self._request_page()
        except Exception as exc:
            self._fail(str(exc))

    def _fail(self, message):
        self.job = None
        QgsMessageLog.logMessage(f"{message}\nWFS: {self.family.wfs}", "QuickNMT", Qgis.MessageLevel.Warning)
        self.failed.emit(message)

    def cancel(self):
        self.cancelled = True
        if self.job is not None:
            self.job.cancel()
            self.job = None
