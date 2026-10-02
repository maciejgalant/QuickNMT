"""Polygon preparation and metric AOI buffers, independent of GUI widgets.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import math

from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsCoordinateTransformContext, QgsFeatureRequest, QgsGeometry,
)
from .models import AoiResult

WORKING_CRS = "EPSG:2180"
MAP_CRS = "EPSG:3857"


def geometry_from_wkb(data):
    geometry = QgsGeometry()
    geometry.fromWkb(data)
    if geometry.isNull():
        raise ValueError("Nie udało się odczytać geometrii obszaru.")
    return geometry


def polygon_parts(geometry):
    if geometry.type() == Qgis.GeometryType.Polygon:
        return [geometry]
    # makeValid can return a collection with collapsed lines or points.
    if geometry.isMultipart():
        parts = []
        for child in geometry.asGeometryCollection():
            parts.extend(polygon_parts(child))
        return parts
    return []


def clean_polygon(geometry):
    if geometry.isNull() or geometry.isEmpty():
        raise ValueError("Obszar jest pusty.")
    geometry = QgsGeometry(geometry)
    geometry.get().dropZValue()
    geometry.get().dropMValue()
    repaired = not geometry.isGeosValid()
    if repaired:
        geometry = geometry.makeValid()
    parts = polygon_parts(geometry)
    if not parts:
        raise ValueError("Geometria nie zawiera poprawnego poligonu po naprawie.")
    geometry = QgsGeometry.unaryUnion(parts) if len(parts) > 1 else QgsGeometry(parts[0])
    if geometry.isEmpty() or not geometry.isGeosValid() or geometry.area() <= 0:
        raise ValueError("Nie udało się naprawić geometrii poligonowej.")
    if any(not math.isfinite(v.x()) or not math.isfinite(v.y()) for v in geometry.vertices()):
        raise ValueError("Geometria zawiera nieprawidłowe współrzędne.")
    return geometry, repaired


def transform_polygon(geometry, source_crs, target_crs, context):
    source = QgsCoordinateReferenceSystem(source_crs)
    target = QgsCoordinateReferenceSystem(target_crs)
    if not source.isValid() or not target.isValid():
        raise ValueError("Brak poprawnego układu współrzędnych obszaru.")
    geometry, repaired = clean_polygon(geometry)
    if source != target:
        transform = QgsCoordinateTransform(source, target, QgsCoordinateTransformContext(context))
        # The AOI was explicitly reduced to XY above. No height transformation.
        geometry.transform(transform)
    result, changed = clean_polygon(geometry)
    return result, repaired or changed


def extent_geometry(rectangle):
    if rectangle.isEmpty() or not rectangle.isFinite():
        raise ValueError("Bieżący zasięg mapy jest pusty lub nieprawidłowy.")
    # Densify the four edges before changing projection, including geographic CRS.
    return QgsGeometry.fromRect(rectangle).densifyByCount(32)


class CancelledError(Exception):
    """Expected cancellation; not a failed download or damaged AOI."""


def build_aoi(geometries, source_crs, context, buffer_m, mode,
              cancelled=lambda: False, progress=lambda value: None):
    if not 0 <= buffer_m <= 500:
        raise ValueError("Bufor musi mieścić się w zakresie 0–500 m.")
    parts = []
    repaired_count = 0
    empty_count = 0
    for index, geometry in enumerate(geometries):
        if cancelled():
            raise CancelledError()
        if geometry.isNull() or geometry.isEmpty():
            empty_count += 1
            continue
        transformed, repaired = transform_polygon(geometry, source_crs, WORKING_CRS, context)
        repaired_count += int(repaired)
        parts.append(transformed)
        if index % 50 == 0:
            progress(min(55, 5 + index / 20))
    if not parts:
        raise ValueError("Nie znaleziono niepustych poligonów w wybranym obszarze.")
    if cancelled():
        raise CancelledError()
    progress(60)
    original, repaired_union = clean_polygon(QgsGeometry.unaryUnion(parts))
    if cancelled():
        raise CancelledError()
    progress(80)
    buffered = original.buffer(buffer_m, 16) if buffer_m > 0 else QgsGeometry(original)
    buffered, _ = clean_polygon(buffered)
    if cancelled():
        raise CancelledError()
    warnings = []
    if repaired_count or repaired_union:
        warnings.append(f"Naprawiono niepoprawne geometrie: {repaired_count + int(repaired_union)}.")
    if empty_count:
        warnings.append(f"Pominięto puste geometrie: {empty_count}.")
    progress(100)
    return AoiResult(bytes(original.asWkb()), bytes(buffered.asWkb()), buffer_m, mode,
                     len(parts), repaired_count + int(repaired_union), original.area(), tuple(warnings))


def feature_geometries(source, selected_ids=None):
    request = QgsFeatureRequest().setNoAttributes()
    if selected_ids is not None:
        if not selected_ids:
            raise ValueError("W warstwie nie ma zaznaczonych obiektów. Zaznacz obiekty albo wyłącz ograniczenie.")
        request.setFilterFids(selected_ids)
    for feature in source.getFeatures(request):
        yield QgsGeometry(feature.geometry())
