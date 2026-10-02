# QuickNMT

**QuickNMT** is a QGIS plugin for downloading, mosaicking and clipping Polish **NMT** (Digital Terrain Model) and **NMPT** (Digital Surface Model) data published through **PZGiK / Geoportal**.

Current release: **0.0.7**  
License: **GPL-3.0-or-later**  
QGIS: **3.36+**, with `qgisMaximumVersion=4.99`

## Main features

- NMT and NMPT source products.
- PL-EVRF2007-NH and PL-KRON86-NH vertical reference systems.
- AOI from the current QGIS map view, a polygon layer, or the interactive Geoportal-style selector.
- Selection by source sheets, rectangle or polygon.
- Automatic technical buffer around the AOI to protect analyses near raster edges.
- Automatic discovery and download of all required source sheets.
- Mosaicking and clipping to the requested AOI.
- Output formats: **GeoTIFF (.tif), Arc/Info ASCII Grid (.asc), XYZ (.xyz), TXT (.txt)**.
- Optional addition of raster outputs to the QGIS project.
- Sidecar `.quicknmt.json` metadata describing the source products and processing decisions.
- Background tasks, progress reporting, cancellation and temporary-file cleanup.
- Large-area support with tiled WFS queries and longer network timeouts.

## Large-area processing

Since version **0.0.6**, QuickNMT no longer sends one very large WFS bounding box for large AOIs. The buffered AOI is split into smaller query tiles of about **60 × 60 km** and the returned sheet records are deduplicated before download.

Source rasters are still downloaded sheet by sheet. WFS requests use an idle timeout of **180 s**, raster downloads allow up to **600 s** of inactivity, and failed downloads can be retried up to **6 times**. The working directory for a large mosaic is created on the same drive as the destination file to reduce the risk of filling the system temporary directory.

The final raster has a safety limit of **1 billion cells**. Very large exports can still require substantial disk space and should be split into several outputs when practical.

## Installation

### QGIS Plugin Repository

After the plugin is approved in the official QGIS Plugin Repository:

1. Open **Plugins → Manage and Install Plugins**.
2. Search for **QuickNMT**.
3. Click **Install Plugin**.

### Install from ZIP

1. Download the release ZIP.
2. In QGIS open **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Select the QuickNMT ZIP file.
4. Install or replace the existing version.

### Clone from GitHub

```bash
git clone https://github.com/maciejgalant/QuickNMT.git
```

For development, place or link the repository directory in the QGIS Python plugins directory so that QGIS sees a folder named `QuickNMT` containing `metadata.txt` and `__init__.py`.

## Basic workflow

1. Choose **NMT** or **NMPT**.
2. Choose the vertical reference system and source resolution/product.
3. Define the AOI from the QGIS view, a polygon layer, or the interactive map selector.
4. Keep the automatic technical buffer or set a custom value.
5. Choose the output format and destination file.
6. Click **Download and merge**.
7. QuickNMT discovers the required source sheets, downloads them, validates them, builds a mosaic, clips the result and writes the final output.

## Output formats

| Format | Content | Automatic QGIS loading |
|---|---|---|
| GeoTIFF `.tif` | Raster with CRS and NoData | Yes, optional |
| ASC `.asc` | Arc/Info ASCII Grid plus `.prj` | Yes, optional |
| XYZ `.xyz` | `X Y Z`, space-separated, no header | No |
| TXT `.txt` | `X<TAB>Y<TAB>Z`, with header | No |

XYZ and TXT exports omit NoData cells and use cell-center coordinates.

## Geodetic data integrity

QuickNMT distinguishes the **horizontal CRS** from the **vertical reference system**. It does **not** convert heights between PL-EVRF2007-NH and PL-KRON86-NH. Instead, it selects the corresponding source product.

The plugin also avoids presenting resampled data as if it were a higher-resolution source. If source rasters have different grids or resolutions, QuickNMT asks for an explicit output-grid decision and records resampling information in the `.quicknmt.json` metadata.

The technical AOI buffer is designed to keep neighbouring elevation cells available near the requested boundary, which is useful for operations such as contour generation.

## Data sources

QuickNMT uses official PZGiK / Geoportal services and source data. OpenStreetMap may be used only as an orientation basemap in the selector.

Useful references:

- Geoportal WFS: https://www.geoportal.gov.pl/pl/usluga/uslugi-pobierania-wfs/
- Geoportal WMS/WMTS: https://www.geoportal.gov.pl/pl/usluga/uslugi-przegladania-wms-i-wmts/
- Geoportal NMT: https://www.geoportal.gov.pl/pl/dane/numeryczny-model-terenu-nmt/
- Geoportal NMPT: https://www.geoportal.gov.pl/pl/dane/numeryczny-model-pokrycia-terenu-nmpt/
- OpenStreetMap copyright: https://www.openstreetmap.org/copyright

## Reporting issues

When reporting a problem, include:

- QGIS version,
- QuickNMT version,
- NMT/NMPT product,
- vertical reference system,
- approximate AOI,
- output format,
- messages from **QGIS Message Log → QuickNMT**.

Issue tracker: https://github.com/maciejgalant/QuickNMT/issues

## Repository

Source code: https://github.com/maciejgalant/QuickNMT

## License

QuickNMT is released under the **GNU General Public License v3.0 or later**. See [`LICENSE`](LICENSE).

Author: **Maciej Galant**
