"""QuickNMT — a standalone QGIS plugin. SPDX-License-Identifier: GPL-3.0-or-later"""

__version__ = "0.0.6"


def classFactory(iface):
    from .quicknmt import QuickNMT
    return QuickNMT(iface)
