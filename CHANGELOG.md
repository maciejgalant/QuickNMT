# Changelog

All notable changes to QuickNMT are documented here.

## 0.0.7

- Replaced the previous network XML parser with `QXmlStreamReader`.
- Explicitly reject DTD and entity-reference XML and added XML size/depth/element limits.
- Preserved safe GML serialization for QGIS geometry parsing without reparsing network XML.
- Reworked official Geoportal endpoint construction into readable path segments to remove false-positive high-entropy/secret warnings in repository audits.
- Kept `experimental=False`.

## 0.0.6

- Marked the release as stable (`experimental=False`).
- Added reliable large-area processing by splitting buffered AOIs into smaller WFS query tiles.
- Deduplicated sheet records returned on tile boundaries.
- Increased WFS idle timeout to 180 seconds.
- Increased raster-download idle timeout to 600 seconds.
- Increased raster download retry count to 6 attempts.
- Increased the final-raster safety limit to 1 billion cells.
- Moved large-mosaic working data to the same drive as the destination file.
- Preserved sequential source-sheet downloads to avoid oversized single requests.

## 0.0.5

- Added mixed-resolution handling.
- Added NMPT 0.5 m / 1.0 m source-product handling.
- Added additional source-product validation.
