"""Thread-local GDAL configuration; never change the host application's defaults.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from contextlib import contextmanager
from osgeo import gdal


@contextmanager
def local_gdal_options(**values):
    previous = {key: gdal.GetThreadLocalConfigOption(key) for key in values}
    try:
        for key, value in values.items():
            gdal.SetThreadLocalConfigOption(key, value)
        yield
    finally:
        for key, value in previous.items():
            gdal.SetThreadLocalConfigOption(key, value)
