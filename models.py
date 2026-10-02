"""Value objects shared by AOI and map widgets.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass, field


@dataclass
class MapSelection:
    geometry_wkb: bytes
    crs: str
    mode: str
    product_key: tuple
    sheets: tuple = ()


@dataclass
class AoiResult:
    original_wkb: bytes
    buffered_wkb: bytes
    buffer_m: float
    mode: str
    source_count: int
    repaired_count: int
    area_m2: float
    warnings: tuple = ()
    crs: str = "EPSG:2180"


@dataclass
class SheetRecord:
    sheet_id: str
    year: int
    geometry_wkb: bytes
    resolution: float
    download_url: str
    source_layer: str
    attributes: dict = field(default_factory=dict)

    @property
    def key(self):
        return self.sheet_id or self.download_url
