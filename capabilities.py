"""Capability parsing/cache needed by the stage 2 map; no fixed year layers.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
import re
import time
import xml.etree.ElementTree as ET

CACHE_SECONDS = 3600
_CACHE = {}


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(payload):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("Nieobsługiwany dokument XML usługi.")
    root = ET.fromstring(payload)
    errors = [" ".join(e.itertext()).strip() for e in root.iter()
              if local_name(e.tag) in ("ExceptionText", "ServiceException")]
    if errors:
        raise ValueError("Usługa zgłosiła błąd: " + "; ".join(errors)[:500])
    return root


def year_from_name(name):
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", name)
    return max(map(int, years), default=0)


@dataclass(frozen=True)
class Capabilities:
    kind: str
    names: tuple
    crs: tuple


def parse_capabilities(payload, kind):
    root = parse_xml(payload)
    expected = {"WMS": "WMS_Capabilities", "WFS": "WFS_Capabilities"}[kind]
    if local_name(root.tag) != expected:
        raise ValueError("Geoportal nie zwrócił oczekiwanego GetCapabilities.")
    container = "Layer" if kind == "WMS" else "FeatureType"
    names = []
    for element in root.iter():
        if local_name(element.tag) == container:
            for child in element:
                if local_name(child.tag) == "Name" and child.text:
                    names.append(child.text.strip())
    crs = tuple(sorted({e.text.strip() for e in root.iter() if e.text and
                        local_name(e.tag) in ("CRS", "DefaultCRS", "OtherCRS")}))
    if not names or not any(c.endswith(":3857") for c in crs):
        raise ValueError("Skorowidz nie udostępnia warstw lub wymaganego CRS EPSG:3857.")
    return Capabilities(kind, tuple(dict.fromkeys(names)), crs)


def cached(endpoint, kind):
    entry = _CACHE.get((endpoint, kind))
    return entry[1] if entry and time.monotonic() - entry[0] < CACHE_SECONDS else None


def remember(endpoint, kind, payload):
    value = parse_capabilities(payload, kind)
    _CACHE[(endpoint, kind)] = (time.monotonic(), value)
    return value
