"""Final format conversion; full precision, bounded memory and cancellable writes.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import math
from pathlib import Path
import numpy as np
from osgeo import gdal, osr
from .raster_source import check_cancel
from .gdal_options import local_gdal_options

EXTENSIONS = {'GeoTIFF': '.tif', 'AAIGrid': '.asc', 'XYZ': '.xyz', 'TXT': '.txt'}
TEXT_FORMATS = frozenset(('XYZ', 'TXT'))
BIG_TEXT_CELLS = 5_000_000
ESTIMATED_BYTES_PER_POINT = 72
BLOCK_CELLS = 32768


def validate_format_path(path, output_format):
    if output_format not in EXTENSIONS:
        raise ValueError('Nieobsługiwany format wyniku.')
    if Path(path).suffix.lower() != EXTENSIONS[output_format]:
        raise ValueError('Rozszerzenie wyniku nie odpowiada formatowi wybranemu na liście.')


def output_files(path, output_format='GeoTIFF'):
    path = Path(path)
    files = [path, path.with_suffix('.quicknmt.json')]
    if output_format == 'AAIGrid':
        files.append(path.with_suffix('.prj'))
    return files


def point_blocks(dataset, cancelled=lambda: False):
    """Yield X/Y pixel centres and valid Z in raster row order, at most 32768 points."""
    width, height = dataset.RasterXSize, dataset.RasterYSize
    gt = dataset.GetGeoTransform()
    for row in range(height):
        for column in range(0, width, BLOCK_CELLS):
            check_cancel(cancelled)
            count = min(BLOCK_CELLS, width - column)
            values = dataset.ReadAsArray(column, row, count, 1)
            if values is None:
                raise ValueError('Nie udało się odczytać wysokości do eksportu tekstowego.')
            positions = np.flatnonzero(np.isfinite(values[0]))
            columns = positions + column + .5
            y = row + .5
            points = np.column_stack((gt[0] + columns * gt[1] + y * gt[2],
                                      gt[3] + columns * gt[4] + y * gt[5], values[0, positions]))
            yield points, (row * width + column + count) / (width * height)


def write_points(dataset, target, output_format, cancelled, progress):
    separator = '\t' if output_format == 'TXT' else ' '
    count = 0
    with Path(target).open('w', encoding='utf-8', newline='\n') as stream:
        if output_format == 'TXT':
            stream.write('X\tY\tZ\n')
        for points, fraction in point_blocks(dataset, cancelled):
            # 17 significant digits round-trip binary64; locale never changes the decimal dot.
            np.savetxt(stream, points, fmt='%.17g', delimiter=separator, newline='\n')
            count += len(points)
            progress(70 * fraction)
    return count


def verify_points(dataset, target, output_format, cancelled, progress):
    separator = '\t' if output_format == 'TXT' else ' '
    count = 0
    with Path(target).open('r', encoding='utf-8', newline='') as stream:
        if output_format == 'TXT' and stream.readline() != 'X\tY\tZ\n':
            raise ValueError('Niepoprawny nagłówek pliku TXT.')
        for expected, fraction in point_blocks(dataset, cancelled):
            lines = [stream.readline() for _ in range(len(expected))]
            if any(not line.endswith('\n') or len(line.rstrip('\n').split(separator)) != 3 for line in lines):
                raise ValueError('Niepełny lub niepoprawny zapis punktów X/Y/Z.')
            if lines:
                try:
                    actual = np.loadtxt(lines, delimiter=separator, dtype=np.float64, ndmin=2)
                except (ValueError, OverflowError) as exc:
                    raise ValueError('Plik tekstowy zawiera niepoprawną liczbę.') from exc
                if actual.shape != expected.shape or not np.array_equal(actual, expected):
                    raise ValueError('Współrzędne lub wysokości pliku tekstowego różnią się od rastra.')
            count += len(expected)
            progress(70 + 30 * fraction)
        if stream.read(1):
            raise ValueError('Plik tekstowy zawiera nadmiarowe punkty.')
    return count


def verify_ascii(source, target, cancelled, progress):
    # AAIGrid normally autodetects Float32. Read as Float64 here to verify all
    # decimal digits saved in the file, also when the working raster is Float64.
    with local_gdal_options(AAIGRID_DATATYPE='Float64', GDAL_PAM_ENABLED='NO'):
        output = gdal.OpenEx(str(target), gdal.OF_RASTER | gdal.OF_READONLY, allowed_drivers=['AAIGrid'])
        try:
            if output is None or output.RasterXSize != source.RasterXSize or output.RasterYSize != source.RasterYSize:
                raise ValueError('Nie udało się sprawdzić wymiarów ASC.')
            if not np.allclose(output.GetGeoTransform(), source.GetGeoTransform(), rtol=0, atol=1e-8):
                raise ValueError('Georeferencja ASC różni się od rastra roboczego.')
            spatial = output.GetSpatialRef()
            reference = source.GetSpatialRef()
            if spatial is None or reference is None:
                raise ValueError('ASC nie otwiera się z poprawnym CRS.')
            for srs in (spatial, reference):
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not spatial.IsSame(reference):
                raise ValueError('CRS zapisanego ASC jest niezgodny ze źródłem.')
            nodata = output.GetRasterBand(1).GetNoDataValue()
            if nodata is None or not math.isnan(nodata):
                raise ValueError('Nie udało się zachować NoData w ASC.')
            rows = max(1, min(256, 1_000_000 // source.RasterXSize))
            for row in range(0, source.RasterYSize, rows):
                check_cancel(cancelled)
                height = min(rows, source.RasterYSize - row)
                expected = source.ReadAsArray(0, row, source.RasterXSize, height)
                actual = output.ReadAsArray(0, row, source.RasterXSize, height)
                if expected is None or actual is None or not np.array_equal(expected, actual, equal_nan=True):
                    raise ValueError('Wysokości lub NoData ASC różnią się od rastra roboczego.')
                progress(75 + 25 * (row + height) / source.RasterYSize)
        finally:
            output = None


def export_raster(source_path, target, output_format, cancelled=lambda: False, progress=lambda p: None):
    validate_format_path(target, output_format)
    source = gdal.OpenEx(str(source_path), gdal.OF_RASTER | gdal.OF_READONLY, allowed_drivers=['GTiff'])
    if source is None:
        raise ValueError('Nie można otworzyć rastra roboczego do konwersji.')
    result = None
    try:
        check_cancel(cancelled)
        if output_format in TEXT_FORMATS:
            written = write_points(source, target, output_format, cancelled, progress)
            verified = verify_points(source, target, output_format, cancelled, progress)
            if written != verified:
                raise ValueError('Nie udało się sprawdzić wszystkich punktów wyniku.')
            return {'point_count': written, 'coordinate_location': 'pixel_center', 'nodata_omitted': True,
                    'header': output_format == 'TXT', 'separator': 'tab' if output_format == 'TXT' else 'space',
                    'encoding': 'UTF-8', 'significant_digits': 17}
        if output_format != 'AAIGrid':
            raise ValueError('Konwersja wymaga formatu ASC, XYZ albo TXT.')
        with local_gdal_options(GDAL_PAM_ENABLED='NO'):
            result = gdal.Translate(str(target), source, format='AAIGrid',
                creationOptions=['SIGNIFICANT_DIGITS=17'],
                callback=lambda fraction, message, data: _conversion_progress(fraction, cancelled, progress))
            check_cancel(cancelled)
            if result is None:
                raise ValueError('Nie udało się zapisać ASC: ' + gdal.GetLastErrorMsg())
            result = None
        # Keep EPSG authorities; GDAL's ESRI-style PRJ can lose them.
        Path(target).with_suffix('.prj').write_text(source.GetProjection(), encoding='utf-8')
        verify_ascii(source, target, cancelled, progress)
        return {'significant_digits': 17, 'crs_file': Path(target).with_suffix('.prj').name,
                'nodata_omitted': False, 'verification_read_type': 'Float64'}
    finally:
        result = None
        source = None


def _conversion_progress(fraction, cancelled, progress):
    if cancelled():
        return False
    progress(75 * fraction)
    return True
