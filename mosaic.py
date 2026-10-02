"""Validated sources -> logical mosaic -> aligned mask -> final format and publication.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import traceback

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask, QgsMessageLog, Qgis
from . import __version__
from .aoi import CancelledError, geometry_from_wkb, transform_polygon
from .raster_source import check_cancel, sha256_file
from .export_formats import export_raster, output_files, validate_format_path
from .sheet_index import matches_product
from .gdal_options import local_gdal_options

MAX_OUTPUT_CELLS = 1_000_000_000
CREATION_OPTIONS = ['TILED=YES', 'COMPRESS=DEFLATE', 'PREDICTOR=3', 'BIGTIFF=IF_SAFER']


def horizontal_srs(wkt):
    spatial = osr.SpatialReference()
    spatial.ImportFromWkt(wkt)
    spatial.StripVertical()
    spatial.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return spatial


def grid_plan(sources, output_resolution=None):
    if not sources:
        raise ValueError('Nie znaleziono arkuszy dla tego obszaru.')
    first = sources[0]['metadata']
    reference = horizontal_srs(first['crs_wkt'])
    resolutions = []
    for source in sources:
        pixels = source['metadata']['pixel_size_m']
        if (len(pixels) != 2 or any(not math.isfinite(r) or r <= 0 for r in pixels)
                or abs(pixels[0] - pixels[1]) > 1e-7):
            raise ValueError('Niepoprawna lub niekwadratowa siatka źródła.')
        if not any(abs(pixels[0] - r) <= 1e-7 for r in resolutions):
            resolutions.append(pixels[0])
    resolutions.sort()
    mixed_resolution = len(resolutions) > 1
    # This is a proposed grid until the user explicitly chooses its resolution.
    resolution = max(resolutions) if output_resolution is None else float(output_resolution)
    if not math.isfinite(resolution) or not any(abs(resolution - r) <= 1e-7 for r in resolutions):
        raise ValueError('Wybierz rozdzielczość wyniku spośród wykrytych rozdzielczości źródłowych.')
    mixed_crs = shifted = False
    for source in sources:
        meta = source['metadata']
        same = bool(reference.IsSame(horizontal_srs(meta['crs_wkt'])))
        mixed_crs |= not same
        if same:
            for axis in (0, 3):
                offset = (meta['geotransform'][axis] - first['geotransform'][axis]) / resolution
                shifted |= abs(offset - round(offset)) > 1e-6
    if mixed_crs:
        reference = osr.SpatialReference()
        reference.ImportFromEPSG(2180)
        reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    reasons = []
    if mixed_resolution:
        reasons.append('Arkusze mają różne rozdzielczości źródłowe: ' +
                       ', '.join(f'{r:g} m'.replace('.', ',') for r in resolutions) + '.')
    if mixed_crs:
        reasons.append('Arkusze mają różne układy poziome. Wynik będzie zapisany w EPSG:2180.')
    elif shifted:
        reasons.append('Siatki arkuszy są przesunięte względem siebie. Wynik zostanie wyrównany do wspólnej siatki.')
    return {'crs_wkt': reference.ExportToWkt(), 'resolution': resolution,
            'source_resolutions': resolutions, 'mixed_resolution': mixed_resolution,
            'resolution_choice_required': mixed_resolution and output_resolution is None,
            'resolution_selected_by_user': mixed_resolution and output_resolution is not None,
            'resampled': mixed_crs or shifted or mixed_resolution,
            'mixed_crs': mixed_crs, 'shifted_grid': shifted, 'reason': ' '.join(reasons)}


def validate_source_products(sources, family, choice):
    """Recheck product identity at the mosaic boundary, including cached metadata."""
    if choice not in family.choices:
        raise ValueError('Rozdzielczość nie należy do wybranego produktu.')
    for source in sources:
        record, meta = source['record'], source['metadata']
        if not matches_product(record.attributes, family, choice):
            raise ValueError('Nie wolno łączyć różnych rodzajów danych lub układów wysokościowych.')
        if any(abs(r - record.resolution) > 1e-5 for r in meta['pixel_size_m']):
            raise ValueError('Rozdzielczość źródła jest niezgodna ze sprawdzonym rastrem.')
        for key, expected in (('data_type', family.data_type), ('vertical_datum', family.vertical_datum)):
            if key in meta and meta[key] != expected:
                raise ValueError('Opis sprawdzonego źródła zawiera inny produkt lub układ wysokościowy.')


def output_snapshot(path, output_format='GeoTIFF'):
    path = Path(path)
    paths = output_files(path, output_format)
    snapshot = {}
    for item in paths:
        if item.exists() and not item.is_file():
            raise ValueError('Ścieżka wyniku wskazuje katalog: ' + str(item))
        info = item.stat() if item.exists() else None
        snapshot[str(item)] = [info.st_size, info.st_mtime_ns] if info else None
    return snapshot


def commit_pair(staging, target, snapshot, output_format='GeoTIFF'):
    """Each replace is atomic; roll back the bundle if any replacement fails.

    The bundle is not a filesystem transaction (power loss is outside this guarantee).
    Keep recovery copies if rollback itself fails; never silently discard old data.
    """
    target = Path(target)
    if output_snapshot(target, output_format) != snapshot:
        raise ValueError('Plik wynikowy zmienił się w czasie pobierania. Wybierz inną nazwę lub ponów zapis.')
    pairs = [(staging / destination.name, destination) for destination in output_files(target, output_format)]
    if not all(source.is_file() for source, destination in pairs):
        raise ValueError('Brakuje pliku wyniku lub jego opisu. Poprzedni wynik pozostaje zachowany.')
    backups, replaced = {}, []
    for _, destination in pairs:
        if destination.exists():
            backup = staging / (destination.name + '.previous')
            shutil.copy2(destination, backup)
            backups[destination] = backup
    try:
        for source, destination in pairs:
            os.replace(source, destination)
            replaced.append(destination)
    except Exception:
        try:
            for destination in reversed(replaced):
                if destination in backups:
                    os.replace(backups[destination], destination)
                else:
                    destination.unlink()
        except Exception as rollback_error:
            (staging / 'KEEP_RECOVERY').write_text(str(rollback_error), encoding='utf-8')
            raise OSError('Nie udało się przywrócić poprzedniego wyniku. Kopie odzyskiwania: ' + str(staging)) from rollback_error
        raise


def _require(dataset, message):
    if dataset is None:
        raise ValueError(message + ': ' + gdal.GetLastErrorMsg())
    return dataset


@local_gdal_options(AAIGRID_DATATYPE='Float64')
def create_mosaic(sources, aoi, keep_buffer, family, choice, target, context, workspace,
                  snapshot, allow_resampling=False, cancelled=lambda: False, progress=lambda p: None,
                  output_format='GeoTIFF', status_callback=lambda message: None, output_resolution=None):
    validate_format_path(target, output_format)
    validate_source_products(sources, family, choice)
    plan = grid_plan(sources, output_resolution)
    if plan['resolution_choice_required']:
        raise ValueError('Mieszane rozdzielczości wymagają wyboru rozdzielczości wyniku.')
    if plan['resampled'] and not allow_resampling:
        raise ValueError(plan['reason'] + ' Wymagane jest potwierdzenie wyrównania siatki.')
    target, workspace = Path(target), Path(workspace)
    staging = Path(tempfile.mkdtemp(prefix='.QuickNMT-', dir=str(target.parent)))
    band = vector = None
    full = window = mask_ds = output = vector_ds = None
    try:
        check_cancel(cancelled)
        # Source values stay unchanged. Float64 also preserves Int32/UInt32 inputs.
        types = {s['metadata']['datatype'] for s in sources}
        dtype = gdal.GDT_Float32 if types <= {'Byte', 'Int16', 'UInt16', 'Float32'} else gdal.GDT_Float64
        if types & {'Int64', 'UInt64', 'CInt16', 'CInt32', 'CFloat32', 'CFloat64'}:
            raise ValueError('Nieobsługiwany typ wysokości źródłowych.')
        resolution = plan['resolution']
        vrt_paths = []
        # Last source wins where it has data; older data fill only NoData cells.
        ordered = sorted(sources, key=lambda s: (-s['record'].year, s['record'].resolution,
                                                  s['record'].key, s['record'].download_url), reverse=True)
        for index, source in enumerate(ordered):
            check_cancel(cancelled)
            path = workspace / f'source-{index}.vrt'
            spatial = horizontal_srs(source['metadata']['crs_wkt'])
            translated = _require(gdal.Translate(str(path), source['raster'], format='VRT', outputType=dtype,
                                                 outputSRS=spatial.ExportToWkt()), 'Nie udało się przygotować źródła')
            translated = None
            if plan['resampled']:
                warped_path = workspace / f'aligned-{index}.vrt'
                warped = _require(gdal.Warp(str(warped_path), str(path), format='VRT',
                    srcSRS=spatial.ExportToWkt(), dstSRS=plan['crs_wkt'],
                    xRes=resolution, yRes=resolution, targetAlignedPixels=True, outputType=dtype,
                    resampleAlg='near', dstNodata=float('nan'), errorThreshold=0.0,
                    overviewLevel='NONE', options=['-novshift'],
                    callback=lambda value, message, data: not cancelled()), 'Nie udało się wyrównać siatki')
                warped = None
                path = warped_path
            vrt_paths.append(str(path))
        full = _require(gdal.BuildVRT(str(workspace / 'mosaic.vrt'), vrt_paths,
            options=gdal.BuildVRTOptions(resolution='user', xRes=resolution, yRes=resolution,
                                         VRTNodata='nan', resampleAlg='nearest', strict=True)),
            'Nie udało się połączyć arkuszy')
        progress(10)
        # Mask is transformed in XY only, to the actual raster CRS.
        selected = aoi.buffered_wkb if keep_buffer else aoi.original_wkb
        geometry, _ = transform_polygon(geometry_from_wkb(selected), aoi.crs, plan['crs_wkt'], context)
        rect = geometry.boundingBox()
        gt = full.GetGeoTransform()
        left = math.floor((rect.xMinimum() - gt[0]) / resolution + 1e-7)
        right = math.ceil((rect.xMaximum() - gt[0]) / resolution - 1e-7)
        top = math.floor((gt[3] - rect.yMaximum()) / resolution + 1e-7)
        bottom = math.ceil((gt[3] - rect.yMinimum()) / resolution - 1e-7)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ValueError('Obszar jest mniejszy niż komórka rastra. Wybierz większy obszar lub bufor.')
        if width * height > MAX_OUTPUT_CELLS:
            raise ValueError('Wynik przekroczyłby 1 miliard komórek. Dla tak dużego rastra podziel eksport na kilka osobnych plików.')
        window = _require(gdal.Translate(str(workspace / 'window.vrt'), full, format='VRT',
            srcWin=[left, top, width, height]), 'Nie udało się przyciąć mozaiki')
        out_gt = window.GetGeoTransform()
        mask_ds = _require(gdal.GetDriverByName('GTiff').Create(str(workspace / 'mask.tif'), width, height,
            1, gdal.GDT_Byte, options=['TILED=YES', 'COMPRESS=DEFLATE']), 'Nie udało się utworzyć maski')
        mask_ds.SetGeoTransform(out_gt)
        mask_ds.SetProjection(plan['crs_wkt'])
        vector_ds = ogr.GetDriverByName('Memory').CreateDataSource('mask')
        vector = vector_ds.CreateLayer('aoi', horizontal_srs(plan['crs_wkt']), ogr.wkbUnknown)
        feature = ogr.Feature(vector.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geometry.asWkb())))
        if vector.CreateFeature(feature) != 0:
            raise ValueError('Nie udało się przygotować maski obszaru.')
        feature = None
        if gdal.RasterizeLayer(mask_ds, [1], vector, burn_values=[1],
                               callback=lambda value, message, data: not cancelled()) != 0:
            check_cancel(cancelled)
            raise ValueError('Nie udało się nałożyć maski obszaru.')
        check_cancel(cancelled)
        raster = staging / target.name if output_format == 'GeoTIFF' else workspace / 'clipped.tif'
        output = _require(gdal.GetDriverByName('GTiff').Create(str(raster), width, height, 1,
            dtype, options=CREATION_OPTIONS), 'Nie udało się utworzyć GeoTIFF')
        output.SetGeoTransform(out_gt)
        output.SetProjection(plan['crs_wkt'])
        band = output.GetRasterBand(1)
        band.SetNoDataValue(float('nan'))
        band.SetUnitType('m')
        output.SetMetadataItem('QUICKNMT_VERTICAL_DATUM', family.vertical_datum)
        rows = max(1, min(256, 1_000_000 // width))
        inside = valid = 0
        minimum, maximum = math.inf, -math.inf
        for row in range(0, height, rows):
            check_cancel(cancelled)
            count = min(rows, height - row)
            values = window.ReadAsArray(0, row, width, count)
            mask = mask_ds.ReadAsArray(0, row, width, count)
            if values is None or mask is None:
                raise ValueError('Nie udało się odczytać fragmentu mozaiki.')
            covered = mask == 1
            inside += int(np.count_nonzero(covered))
            values[~covered] = np.nan
            present = np.isfinite(values)
            valid += int(np.count_nonzero(present))
            if present.any():
                minimum = min(minimum, float(values[present].min()))
                maximum = max(maximum, float(values[present].max()))
            if band.WriteArray(values, 0, row) != 0:
                raise ValueError('Nie udało się zapisać danych. Sprawdź miejsce na dysku.')
            progress(15 + 45 * (row + count) / height)
        if inside == 0:
            raise ValueError('W obszarze nie ma środka żadnej komórki. Powiększ obszar lub bufor.')
        if valid == 0:
            raise ValueError('Dane źródłowe nie pokrywają wskazanego obszaru; wynik zawierałby tylko NoData.')
        if output.FlushCache() not in (None, 0):
            raise ValueError('Nie udało się zakończyć zapisu GeoTIFF.')
        band = None
        output = None
        # Reopen and read every output block before replacing any existing result.
        output = _require(gdal.OpenEx(str(raster), gdal.OF_RASTER | gdal.OF_READONLY), 'Nie można otworzyć wyniku')
        if output.GetGeoTransform() != out_gt or not horizontal_srs(output.GetProjection()).IsSame(horizontal_srs(plan['crs_wkt'])):
            raise ValueError('Georeferencja zapisanego wyniku jest niezgodna z mozaiką.')
        verified = 0
        for row in range(0, height, rows):
            check_cancel(cancelled)
            values = output.ReadAsArray(0, row, width, min(rows, height - row))
            if values is None or np.isinf(values).any():
                raise ValueError('Nie udało się sprawdzić zapisanego wyniku.')
            verified += int(np.count_nonzero(np.isfinite(values)))
            progress(60 + 10 * (row + min(rows, height - row)) / height)
        if verified != valid:
            raise ValueError('Liczba zapisanych komórek jest niezgodna z mozaiką.')
        output = None
        export_details = {}
        if output_format != 'GeoTIFF':
            status_callback(f'Zapisuję i sprawdzam wynik {output_format}…')
            export_details = export_raster(raster, staging / target.name, output_format, cancelled,
                                          lambda value: progress(70 + .24 * value))
        final_file = staging / target.name
        coverage = 100 * valid / inside
        warnings = list(aoi.warnings)
        if valid < inside:
            warnings.append(f'Dane źródłowe nie pokrywają całego wskazanego obszaru. Pokrycie: {coverage:.2f}%. Braki pozostawiono jako NoData.')
        if plan['mixed_resolution']:
            warnings.append('Połączono różne rozdzielczości źródłowe. Piksel wyniku '
                            f'{resolution:g} m nie zwiększa szczegółowości źródeł o większym pikselu.')
        metadata = {'plugin': 'QuickNMT', 'version': __version__, 'stage': 6,
            'created_utc': datetime.now(timezone.utc).isoformat(), 'data_type': family.data_type,
            'vertical_datum': family.vertical_datum, 'height_transformation': False,
            'source_product': choice.key, 'source_resolution': sorted({s['record'].resolution for s in sources}),
            'output_resolution': resolution, 'resampled': plan['resampled'],
            'mixed_source_resolutions': plan['mixed_resolution'],
            'output_resolution_selected_by_user': plan['resolution_selected_by_user'],
            'resampling_method': 'nearest' if plan['resampled'] else None,
            'resampling_confirmed': bool(allow_resampling and plan['resampled']),
            'horizontal_crs_wkt': plan['crs_wkt'], 'grid': plan, 'geotransform': list(out_gt),
            'size': [width, height], 'datatype': gdal.GetDataTypeName(dtype), 'nodata': 'nan',
            'minimum_z': minimum, 'maximum_z': maximum, 'valid_cells': valid,
            'aoi_cells': inside, 'coverage_percent': coverage, 'coverage_method': 'pixel_centers',
            'buffer_m': aoi.buffer_m, 'keep_buffer': keep_buffer, 'search_uses_buffer': True,
            'aoi': {'crs': aoi.crs, 'mode': aoi.mode,
                    'original_wkt': geometry_from_wkb(aoi.original_wkb).asWkt(),
                    'buffered_wkt': geometry_from_wkb(aoi.buffered_wkb).asWkt()},
            'format': output_format, 'export': export_details, 'output_file': target.name,
            'source_years': sorted({s['record'].year for s in sources}),
            'services': {'wfs': family.wfs, 'wms': family.wms},
            'sources': [{'sheet_id': s['record'].sheet_id, 'year': s['record'].year,
                         'source_resolution': s['record'].resolution,
                         'url': s['record'].download_url, 'metadata': s['metadata']} for s in sources],
            'warnings': warnings, 'output_sha256': sha256_file(final_file, cancelled)}
        if output_format in ('GeoTIFF', 'AAIGrid'):
            metadata['raster_sha256'] = metadata['output_sha256']
        (staging / target.with_suffix('.quicknmt.json').name).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        check_cancel(cancelled)
        commit_pair(staging, target, snapshot, output_format)
        # Commit is final; cancellation after this point must report the saved result.
        return {'raster': str(target), 'file': str(target), 'is_raster': output_format in ('GeoTIFF', 'AAIGrid'),
                'folder': str(target.parent), 'metadata': metadata, 'warnings': warnings}
    finally:
        band = vector = None
        full = window = mask_ds = output = vector_ds = None
        if staging.exists() and not (staging / 'KEEP_RECOVERY').exists():
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                QgsMessageLog.logMessage(f'Sprzątanie {staging}: {exc}', 'QuickNMT', Qgis.MessageLevel.Warning)


class MosaicTask(QgsTask):
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(self, **arguments):
        super().__init__('QuickNMT — łączenie i przycinanie rastrów', QgsTask.Flag.CanCancel)
        self.arguments = arguments
        self.result, self.error = None, ''

    def run(self):
        try:
            self.result = create_mosaic(**self.arguments, cancelled=self.isCanceled, progress=self.setProgress,
                                        status_callback=self.status_message.emit)
            return True
        except CancelledError:
            return False
        except Exception as exc:
            self.error = str(exc)
            QgsMessageLog.logMessage(traceback.format_exc(), 'QuickNMT', Qgis.MessageLevel.Warning)
            return False

    def finished(self, success):
        if self.result is not None:
            self.ready.emit(self.result)
        elif self.isCanceled():
            self.cancelled.emit()
        else:
            self.failed.emit(self.error or 'Nie udało się połączyć rastrów.')
