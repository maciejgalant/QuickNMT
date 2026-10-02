"""Add output rasters to a reusable group, on the GUI thread only.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from pathlib import Path
from qgis.core import QgsProject, QgsRasterLayer


def add_result_layer(path, family):
    layer = QgsRasterLayer(str(path), Path(path).stem, 'gdal')
    if not layer.isValid():
        raise ValueError("Plik zapisany, ale QGIS nie może otworzyć rastra: " + str(path))
    layer.setCustomProperty('QuickNMT/vertical_datum', family.vertical_datum)
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(family.data_type)
    if group is None:
        group = root.addGroup(family.data_type)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    return layer
