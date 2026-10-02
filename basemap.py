"""Native XYZ basemap. QGIS handles visible tiles, HTTP cache and cancellation.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from qgis.core import QgsDataSourceUri, QgsRasterLayer
from .geoportal_services import OSM_TILE_URL, USER_AGENT


def create_osm_layer():
    uri = QgsDataSourceUri()
    for key, value in {"type": "xyz", "url": OSM_TILE_URL, "zmin": "0", "zmax": "19",
                       "http-header:User-Agent": USER_AGENT}.items():
        uri.setParam(key, value)
    layer = QgsRasterLayer(bytes(uri.encodedUri()).decode("utf-8"), "OpenStreetMap", "wms")
    if not layer.isValid():
        raise ValueError("Nie udało się utworzyć podkładu OpenStreetMap.")
    layer.setAttribution("© OpenStreetMap contributors")
    layer.setAttributionUrl("https://www.openstreetmap.org/copyright")
    return layer
