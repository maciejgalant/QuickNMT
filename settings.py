"""Persist GUI preferences under QuickNMT/ only.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from qgis.PyQt.QtCore import QSettings

DEFAULTS = {
    "last_folder": "",
    "data_type": "NMT",
    "vertical_datum": "PL-EVRF2007-NH",
    "resolution": "grid_le_1m",
    "output_format": "GeoTIFF",
    "aoi_mode": "current_extent",
    "manual_buffer_m": 10.0,
    "auto_buffer": True,
    "keep_buffer": True,
    "add_to_project": True,
    "selected_only": False,
}


class SettingsStore:
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else QSettings()

    def get(self, key):
        default = DEFAULTS[key]
        value = self.backend.value("QuickNMT/" + key, default)
        if isinstance(default, bool):
            return str(value).lower() in ("true", "1", "yes")
        if isinstance(default, float):
            try:
                return min(500.0, max(0.0, float(value)))
            except (ValueError, TypeError):
                return default
        return str(value) if value is not None else default

    def save(self, values):
        for key, value in values.items():
            if key not in DEFAULTS:
                raise KeyError(key)
            self.backend.setValue("QuickNMT/" + key, value)
        self.backend.sync()
        if self.backend.status() != QSettings.Status.NoError:
            raise OSError("Nie udało się zapisać ustawień QuickNMT.")
