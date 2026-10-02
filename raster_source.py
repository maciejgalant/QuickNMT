"""Validate and publish one untouched source raster. No warp or resampling.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import traceback
from urllib.parse import unquote, urlsplit
import zipfile

import numpy as np
from osgeo import gdal, osr
from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask, QgsGeometry, QgsRectangle, QgsMessageLog, Qgis
from .aoi import CancelledError, geometry_from_wkb, transform_polygon
from .sheet_index import matches_product
from .network import validate_url
from .gdal_options import local_gdal_options
from . import __version__

RASTER_SUFFIXES = {".asc", ".tif", ".tiff"}
MAX_EXTRACTED_BYTES = 2 * 1024 ** 3


def check_cancel(cancelled):
    if cancelled():
        raise CancelledError()


def copy_file(source, target, cancelled):
    with Path(source).open("rb") as reader, Path(target).open("wb") as writer:
        while True:
            check_cancel(cancelled)
            data = reader.read(1024 * 1024)
            if not data:
                break
            writer.write(data)


def sha256_file(path, cancelled=lambda: False):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            check_cancel(cancelled)
            data = stream.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def safe_extract(archive_path, directory, cancelled=lambda: False):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    total, seen = 0, set()
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > 128:
            raise ValueError("Archiwum zawiera zbyt wiele plików dla jednego arkusza.")
        for entry in archive.infolist():
            check_cancel(cancelled)
            name = entry.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or any(p in ("..", ".") or ":" in p or p.endswith((" ", ".")) for p in parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in parts)):
                raise ValueError("Archiwum zawiera niebezpieczną ścieżkę.")
            target = (directory / name).resolve()
            if directory not in target.parents:
                raise ValueError("Ścieżka archiwum wychodzi poza katalog zadania.")
            key = str(target).casefold()
            if key in seen:
                raise ValueError("Archiwum zawiera powtórzoną ścieżkę.")
            seen.add(key)
            total += entry.file_size
            if total > MAX_EXTRACTED_BYTES or entry.flag_bits & 1:
                raise ValueError("Archiwum jest zaszyfrowane lub przekracza limit 2 GiB.")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Extract only data and sidecars, never executables or VRT files.
            if target.suffix.lower() not in RASTER_SUFFIXES | {".prj", ".tfw", ".wld"}:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(entry) as reader, target.open("xb") as writer:
                while True:
                    check_cancel(cancelled)
                    data = reader.read(1024 * 1024)
                    if not data:
                        break
                    written += len(data)
                    if written > entry.file_size or written > MAX_EXTRACTED_BYTES:
                        raise ValueError("Rozmiar pliku ZIP jest niezgodny z nagłówkiem.")
                    writer.write(data)
            if written != entry.file_size:
                raise ValueError("Niepełny plik w archiwum ZIP.")


def expected_horizontal_crs(record):
    name = re.sub(r"[^A-Z0-9]", "", record.attributes.get("uklad_xy", "").upper())
    if name in {"PL1992", "PUWG1992", "PUWG92", "EPSG2180"}:
        return 2180
    # Only explicit WFS zone labels; never infer a CRS from the sheet name
    # or the coordinate magnitudes. PL-2000:S7 is returned by the live WFS.
    match = re.fullmatch(r"(?:PL2000S|PL2000STREFA|PUWG2000S|PUWG2000STREFA)([5-8])", name)
    if match:
        return {"5": 2176, "6": 2177, "7": 2178, "8": 2179}[match.group(1)]
    return {"EPSG2176": 2176, "EPSG2177": 2177, "EPSG2178": 2178, "EPSG2179": 2179}.get(name)


def source_file(download, record, directory, cancelled):
    directory = Path(directory)
    if zipfile.is_zipfile(download):
        safe_extract(download, directory, cancelled)
        rasters = sorted(p for p in directory.rglob('*') if p.suffix.lower() in RASTER_SUFFIXES)
        if len(rasters) != 1:
            raise ValueError(f"Archiwum zawiera {len(rasters)} rastrów. Pobieranie źródła wymaga jednego arkusza rastrowego w ZIP.")
        return rasters[0]
    suffix = Path(unquote(urlsplit(record.download_url).path)).suffix.lower()
    with Path(download).open("rb") as stream:
        signature = stream.read(512)
    if signature.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        actual = ".tif"
    elif signature.lstrip().lower().startswith(b"ncols"):
        actual = ".asc"
    else:
        raise ValueError("Pobrany plik nie jest rozpoznanym ASC, GeoTIFF ani ZIP.")
    if suffix in RASTER_SUFFIXES and ((suffix == ".asc") != (actual == ".asc")):
        raise ValueError("Zawartość pliku jest niezgodna z rozszerzeniem źródła.")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("arkusz" + actual)
    copy_file(download, target, cancelled)
    return target


@local_gdal_options(AAIGRID_DATATYPE='Float64')
def validate_raster(path, record, family, choice, context, cancelled=lambda: False, progress=lambda p: None):
    validate_url(record.download_url)
    if not matches_product(record.attributes, family, choice):
        raise ValueError("Arkusz nie odpowiada wybranemu produktowi, datum lub rozdzielczości.")
    dataset = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY, allowed_drivers=["GTiff", "AAIGrid"])
    if dataset is None:
        raise ValueError("GDAL nie może otworzyć pobranego rastra.")
    try:
        if dataset.RasterCount != 1 or dataset.RasterXSize <= 0 or dataset.RasterYSize <= 0:
            raise ValueError("Arkusz musi zawierać dokładnie jeden niepusty kanał wysokości.")
        if dataset.RasterXSize * dataset.RasterYSize > 100_000_000:
            raise ValueError("Arkusz przekracza limit 100 milionów komórek.")
        driver = dataset.GetDriver().ShortName
        declared = re.sub(r"[^A-Z0-9]", "", record.attributes["format"].upper())
        if (declared == "ARCINFOASCIIGRID") != (driver == "AAIGrid"):
            raise ValueError("Format rastra nie zgadza się ze skorowidzem WFS.")
        spatial = dataset.GetSpatialRef()
        crs_source = "raster"
        horizontal = expected_horizontal_crs(record)
        if spatial is None:
            if driver != "AAIGrid" or horizontal is None:
                raise ValueError("Brak CRS w pliku i jednoznacznego układu poziomego w WFS.")
            spatial = osr.SpatialReference()
            spatial.ImportFromEPSG(horizontal)
            crs_source = "WFS: " + record.attributes["uklad_xy"]
        if not spatial.IsProjected() or abs(spatial.GetLinearUnits() - 1.0) > 1e-8:
            raise ValueError("Raster nie używa metrycznego układu poziomego.")
        if spatial.IsCompound():
            vertical = spatial.GetAttrValue("VERT_CS") or spatial.GetAttrValue("VERTCRS") or ""
            token = "EVRF2007" if "EVRF2007" in family.vertical_datum else "KRON86"
            if token not in re.sub(r"[^A-Z0-9]", "", vertical.upper()):
                raise ValueError("Zapisany w rastrze układ pionowy wymaga potwierdzenia zgodności ze źródłem WFS.")
        if horizontal is not None:
            expected = osr.SpatialReference()
            expected.ImportFromEPSG(horizontal)
            horizontal_part = spatial.Clone()
            horizontal_part.StripVertical()
            # Raster geotransforms use x/y. Compare CRS with the same axis mapping,
            # without modifying either coordinates or stored height values.
            horizontal_part.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            expected.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not horizontal_part.IsSame(expected):
                raise ValueError("CRS rastra nie zgadza się z układem poziomym WFS.")
        gt = dataset.GetGeoTransform(can_return_null=True)
        if gt is None or any(not math.isfinite(v) for v in gt) or gt[1] <= 0 or gt[5] >= 0 or abs(gt[2]) > 1e-8 or abs(gt[4]) > 1e-8:
            raise ValueError("Nieobsługiwana lub niepoprawna georeferencja rastra.")
        pixels = [abs(gt[1]), abs(gt[5])]
        if any(abs(value - record.resolution) > 1e-5 for value in pixels):
            raise ValueError(f"Piksel rastra {pixels} m różni się od źródłowych {record.resolution:g} m w WFS.")
        wkt = spatial.ExportToWkt()
        footprint = QgsGeometry.fromRect(QgsRectangle(gt[0], gt[3] + gt[5] * dataset.RasterYSize,
                                                      gt[0] + gt[1] * dataset.RasterXSize, gt[3]))
        mapped, _ = transform_polygon(footprint, wkt, "EPSG:3857", context)
        sheet = geometry_from_wkb(record.geometry_wkb)
        overlap = mapped.intersection(sheet)
        if overlap.isEmpty() or overlap.area() < .5 * min(mapped.area(), sheet.area()):
            raise ValueError("Zasięg rastra nie odpowiada geometrii arkusza ze skorowidzu.")
        band = dataset.GetRasterBand(1)
        if band.GetScale() not in (None, 1.0) or band.GetOffset() not in (None, 0.0):
            raise ValueError("Raster ma skalowanie wartości Z wymagające osobnej obsługi.")
        nodata = band.GetNoDataValue()
        if nodata is None:
            raise ValueError("Raster nie ma określonej wartości NoData.")
        count, minimum, maximum = 0, math.inf, -math.inf
        block_rows = max(1, min(256, 1_000_000 // dataset.RasterXSize))
        for row in range(0, dataset.RasterYSize, block_rows):
            check_cancel(cancelled)
            height = min(block_rows, dataset.RasterYSize - row)
            data = band.ReadAsArray(0, row, dataset.RasterXSize, height)
            if data is None or data.shape != (height, dataset.RasterXSize):
                raise ValueError("Nie udało się odczytać wszystkich komórek rastra.")
            mask = ~np.isnan(data) if math.isnan(nodata) else data != nodata
            values = data[mask]
            if values.size:
                if not np.isfinite(values).all():
                    raise ValueError("Raster zawiera niepoprawne wartości wysokości.")
                count += int(values.size)
                minimum = min(minimum, float(values.min()))
                maximum = max(maximum, float(values.max()))
            progress(15 + 65 * (row + height) / dataset.RasterYSize)
        if count == 0:
            raise ValueError("Raster zawiera wyłącznie NoData.")
        return {"driver": driver, "crs_wkt": wkt, "crs_source": crs_source,
                "data_type": family.data_type, "vertical_datum": family.vertical_datum,
                "vertical_datum_source": "Geoportal WFS: uklad_h",
                "size": [dataset.RasterXSize, dataset.RasterYSize], "pixel_size_m": pixels,
                "geotransform": list(gt), "bands": 1, "datatype": gdal.GetDataTypeName(band.DataType),
                "nodata": nodata if math.isfinite(nodata) else str(nodata), "valid_cells": count,
                "minimum_z": minimum, "maximum_z": maximum}
    finally:
        dataset = None


def publish_source(download, record, family, choice, output_folder, context, downloaded_sha256,
                   cancelled=lambda: False, progress=lambda p: None):
    parent = Path(output_folder).resolve()
    if not parent.is_dir():
        raise ValueError("Folder zapisu nie istnieje.")
    staging = Path(tempfile.mkdtemp(prefix=".QuickNMT-", dir=str(parent)))
    unpacked = None
    try:
        unpacked = Path(tempfile.mkdtemp(prefix="extracted-", dir=str(Path(download).parent)))
        source = source_file(download, record, unpacked, cancelled)
        metadata = validate_raster(source, record, family, choice, context, cancelled, progress)
        check_cancel(cancelled)
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", record.sheet_id or "arkusz")[:100]
        stem = f"{family.data_type}_{stem}_{record.year}"
        raster = staging / (stem + source.suffix.lower())
        copy_file(source, raster, cancelled)
        if source.suffix.lower() == ".asc":
            spatial = osr.SpatialReference()
            spatial.ImportFromWkt(metadata["crs_wkt"])
            # Keep EPSG authority: ESRI morphing drops it and QGIS may then see
            # a custom unnamed CRS in current EPSG databases.
            raster.with_suffix('.prj').write_text(spatial.ExportToWkt(), encoding="utf-8")
        else:
            for ext in ('.tfw', '.wld'):
                companion = source.with_suffix(ext)
                if companion.is_file():
                    copy_file(companion, raster.with_suffix(ext), cancelled)
        source_hash = sha256_file(source, cancelled)
        if sha256_file(raster, cancelled) != source_hash:
            raise ValueError("Kontrola kopii pliku źródłowego nie powiodła się.")
        # Re-open published layout (including .prj) before committing the directory.
        check = gdal.OpenEx(str(raster), gdal.OF_RASTER | gdal.OF_READONLY, allowed_drivers=["GTiff", "AAIGrid"])
        if check is None or check.GetSpatialRef() is None:
            raise ValueError("Wynikowy plik nie otwiera się z określonym CRS.")
        check = None
        metadata.update({"plugin": "QuickNMT", "version": __version__, "stage": 3,
                         "created_utc": datetime.now(timezone.utc).isoformat(),
                         "sheet_id": record.sheet_id, "year": record.year,
                         "data_type": family.data_type, "vertical_datum": family.vertical_datum,
                         "vertical_datum_source": "Geoportal WFS: uklad_h", "source_layer": record.source_layer,
                         "source_url": record.download_url, "source_attributes": record.attributes,
                         "download_sha256": downloaded_sha256, "raster_sha256": source_hash,
                         "height_transformation": False, "resampling": False, "clipping": False,
                         "extent_mode": "full_source_sheet", "buffer_applied": False})
        (staging / (stem + '.quicknmt.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        progress(95)
        check_cancel(cancelled)
        destination = parent / stem
        counter = 2
        while destination.exists():
            destination = parent / f"{stem}_{counter}"
            counter += 1
        # Same-volume directory rename: nothing is published until validation finishes.
        # Existing results are never overwritten.
        os.rename(staging, destination)
        return {"raster": str(destination / raster.name), "folder": str(destination), "metadata": metadata}
    finally:
        for temporary in (staging, unpacked):
            if temporary is not None and temporary.exists():
                try:
                    shutil.rmtree(temporary)
                except OSError as exc:
                    QgsMessageLog.logMessage(f"Sprzątanie {temporary}: {exc}", "QuickNMT", Qgis.MessageLevel.Warning)


class SourceValidationTask(QgsTask):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, download, record, family, choice, output_folder, context, digest):
        super().__init__("QuickNMT — kontrola i zapis arkusza", QgsTask.Flag.CanCancel)
        self.arguments = (download, record, family, choice, output_folder, context, digest)
        self.result, self.error = None, ""

    def run(self):
        try:
            self.result = publish_source(*self.arguments, self.isCanceled, self.setProgress)
            return True
        except CancelledError:
            return False
        except Exception as exc:
            self.error = str(exc)
            QgsMessageLog.logMessage(traceback.format_exc(), "QuickNMT", Qgis.MessageLevel.Warning)
            return False

    def finished(self, success):
        if self.result is not None:
            self.ready.emit(self.result)
        elif self.isCanceled():
            self.cancelled.emit()
        else:
            self.failed.emit(self.error or "Nie udało się sprawdzić arkusza.")
