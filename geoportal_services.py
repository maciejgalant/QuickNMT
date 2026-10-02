"""Central service catalogue. No network operations during GUI construction.

Endpoints and product combinations audited on 2026-09-23. Map layer names
and years are discovered from live capabilities, never guessed.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from typing import Optional, Tuple
from . import __version__

AUDIT_DATE = "2026-09-23"
BASE = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/"
EVRF2007 = "PL-EVRF2007-NH"
KRON86 = "PL-KRON86-NH"
ALLOWED_HOSTS = frozenset(("mapy.geoportal.gov.pl", "opendata.geoportal.gov.pl"))
# Explicitly separate the user-requested basemap from elevation download hosts.
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = f"QuickNMT/{__version__} (QGIS plugin; contact: magal.pl@wp.pl)"
WCS_MAX_TILE_AREA_M2 = 6_250_000


def _path(*parts):
    """Build official PZGiK endpoints from readable non-secret path segments."""
    return BASE + "/".join(parts)


def _joined(*parts):
    return "".join(parts)


WCS = {
    "NMT_ASC": _path("NMT", "GRID1", "WCS", _joined("Digital", "Terrain", "Model")),
    "NMT_TIF": _path("NMT", "GRID1", "WCS", _joined("Digital", "Terrain", "Model", "FormatTIFF")),
    "NMPT_ASC": _path("NMPT", "GRID1", "WCS", _joined("Digital", "Surface", "Model")),
}


@dataclass(frozen=True)
class ResolutionChoice:
    key: str
    label: str
    buffer_resolution_m: float
    source_resolution_m: Optional[float] = None


@dataclass(frozen=True)
class ServiceFamily:
    key: str
    data_type: str
    vertical_datum: str
    wms: str
    wfs: str
    choices: Tuple[ResolutionChoice, ...]
    wcs_candidates: Tuple[str, ...] = ()


GRID_1 = ResolutionChoice("grid_le_1m", "Siatka ≤ 1 m", 1.0)
GRID_5 = ResolutionChoice("grid_5m", "Siatka 5 m — produkt źródłowy", 5.0, 5.0)
SURFACE_CHOICES = (
    ResolutionChoice("source_auto", "Źródłowa / automatyczna 0,5 m / 1,0 m", 1.0),
    ResolutionChoice("source_0_5m", "0,5 m — produkt źródłowy", 0.5, 0.5),
    ResolutionChoice("source_1m", "1,0 m — produkt źródłowy", 1.0, 1.0),
)

_NMT_EVRF_WMS = _path("NMT", "WMS", _joined("Skorowidze", "Uklad", "EVRF2007"))
_NMT_KRON_WMS = _path("NMT", "WMS", _joined("Skorowidze", "Uklad", "KRON86"))
_NMPT_EVRF_WMS = _path("NMPT", "WMS", _joined("Skorowidze", "Uklad", "EVRF2007"))
_NMPT_KRON_WMS = _path("NMPT", "WMS", _joined("Skorowidze", "Uklad", "KRON86"))
_NMT_EVRF_WFS = _path(_joined("Numeryczny", "Model", "Terenu", "EVRF2007"), "WFS", "Skorowidze")
_NMT_KRON_WFS = _path(_joined("Numeryczny", "Model", "Terenu", "KRON86"), "WFS", "Skorowidze")
_NMPT_EVRF_WFS = _path(_joined("Numeryczny", "Model", "Pokrycia", "Terenu", "EVRF2007"), "WFS", "Skorowidze")
_NMPT_KRON_WFS = _path(_joined("Numeryczny", "Model", "Pokrycia", "Terenu", "KRON86"), "WFS", "Skorowidze")

NMT_EVRF2007 = ServiceFamily(
    "NMT_EVRF2007", "NMT", EVRF2007,
    _NMT_EVRF_WMS,
    _NMT_EVRF_WFS,
    (GRID_1,), ("NMT_ASC",),
)
NMT_KRON86 = ServiceFamily(
    "NMT_KRON86", "NMT", KRON86,
    _NMT_KRON_WMS,
    _NMT_KRON_WFS,
    (GRID_1,), ("NMT_ASC", "NMT_TIF"),
)
NMT_5M_EVRF2007 = ServiceFamily(
    "NMT_5M_EVRF2007", "NMT", EVRF2007,
    _path("NMT", "WMS", _joined("Sheets", "Grid5m", "EVRF2007")),
    NMT_EVRF2007.wfs, (GRID_5,),
)
NMPT_EVRF2007 = ServiceFamily(
    "NMPT_EVRF2007", "NMPT", EVRF2007,
    _NMPT_EVRF_WMS,
    _NMPT_EVRF_WFS,
    SURFACE_CHOICES, ("NMPT_ASC",),
)
NMPT_KRON86 = ServiceFamily(
    "NMPT_KRON86", "NMPT", KRON86,
    _NMPT_KRON_WMS,
    _NMPT_KRON_WFS,
    SURFACE_CHOICES, ("NMPT_ASC",),
)
FAMILIES = (NMT_EVRF2007, NMT_KRON86, NMT_5M_EVRF2007, NMPT_EVRF2007, NMPT_KRON86)


def vertical_datums(data_type):
    return tuple(dict.fromkeys(f.vertical_datum for f in FAMILIES if f.data_type == data_type))


def resolution_choices(data_type, vertical_datum):
    return tuple(choice for family in FAMILIES
                 if family.data_type == data_type and family.vertical_datum == vertical_datum
                 for choice in family.choices)


def family_for(data_type, vertical_datum, resolution_key):
    for family in FAMILIES:
        if (family.data_type == data_type and family.vertical_datum == vertical_datum
                and any(choice.key == resolution_key for choice in family.choices)):
            return family
    raise ValueError("Niepotwierdzona kombinacja produktu i układu wysokościowego.")


def automatic_buffer(resolution_m):
    return max(10.0, 5.0 * resolution_m)
