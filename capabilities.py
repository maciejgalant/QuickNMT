"""Capability parsing/cache needed by the stage 2 map; no fixed year layers.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from html import escape
import re
import time

from qgis.PyQt.QtCore import QByteArray, QXmlStreamReader

CACHE_SECONDS = 3600
_CACHE = {}

_MAX_XML_BYTES = 20 * 1024 * 1024
_MAX_XML_DEPTH = 128
_MAX_XML_ELEMENTS = 250_000
_GML_NAMESPACES = frozenset((
    "http://www.opengis.net/gml",
    "http://www.opengis.net/gml/3.2",
))


class SafeXmlElement:
    """Small immutable-enough XML tree node built by QXmlStreamReader.

    QXmlStreamReader is used for XML received from network services. DTDs and entity references are rejected explicitly.
    Only the small tree API required by QuickNMT is exposed.
    """

    __slots__ = ("tag", "text", "attrib", "children")

    def __init__(self, tag, attrib=None):
        self.tag = tag
        self.text = ""
        self.attrib = dict(attrib or {})
        self.children = []

    def __iter__(self):
        return iter(self.children)

    def __len__(self):
        return len(self.children)

    def __getitem__(self, index):
        return self.children[index]

    def get(self, key, default=None):
        return self.attrib.get(key, default)

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()

    def itertext(self):
        if self.text:
            yield self.text
        for child in self.children:
            yield from child.itertext()


def _expanded_name(namespace_uri, local):
    namespace_uri = str(namespace_uri or "")
    local = str(local)
    return f"{{{namespace_uri}}}{local}" if namespace_uri else local


def _split_expanded_name(name):
    if name.startswith("{") and "}" in name:
        namespace_uri, local = name[1:].split("}", 1)
        return namespace_uri, local
    return "", name


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(payload):
    """Parse untrusted service XML with strict limits and no DTD/entities."""
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("Nieprawidłowy dokument XML usługi.")
    if len(payload) > _MAX_XML_BYTES:
        raise ValueError("Dokument XML usługi jest zbyt duży.")

    # Fast rejection before parsing. QXmlStreamReader checks below remain the
    # authoritative protection and also catch encoded/structured constructs.
    upper = bytes(payload).upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("Nieobsługiwany dokument XML usługi.")

    reader = QXmlStreamReader(QByteArray(bytes(payload)))
    root = None
    stack = []
    element_count = 0

    while not reader.atEnd():
        reader.readNext()

        if reader.isDTD() or reader.isEntityReference():
            raise ValueError("Dokument XML zawiera niedozwoloną deklarację DTD lub encję.")

        if reader.isStartElement():
            element_count += 1
            if element_count > _MAX_XML_ELEMENTS:
                raise ValueError("Dokument XML usługi zawiera zbyt wiele elementów.")
            if len(stack) >= _MAX_XML_DEPTH:
                raise ValueError("Dokument XML usługi jest zbyt głęboko zagnieżdżony.")

            attrs = {}
            for attr in reader.attributes():
                key = _expanded_name(attr.namespaceUri(), attr.name())
                attrs[key] = str(attr.value())

            node = SafeXmlElement(
                _expanded_name(reader.namespaceUri(), reader.name()),
                attrs,
            )
            if stack:
                stack[-1].children.append(node)
            else:
                if root is not None:
                    raise ValueError("Dokument XML ma więcej niż jeden element główny.")
                root = node
            stack.append(node)
            continue

        if reader.isCharacters() and stack:
            # Preserve coordinate whitespace; callers strip ordinary attributes.
            stack[-1].text += str(reader.text())
            continue

        if reader.isEndElement():
            if not stack:
                raise ValueError("Nieprawidłowa struktura XML usługi.")
            stack.pop()

    if reader.hasError():
        raise ValueError("Nie można odczytać XML usługi: " + reader.errorString())
    if root is None or stack:
        raise ValueError("Pusty lub niekompletny dokument XML usługi.")

    errors = [" ".join("".join(e.itertext()).split()).strip() for e in root.iter()
              if local_name(e.tag) in ("ExceptionText", "ServiceException")]
    errors = [message for message in errors if message]
    if errors:
        raise ValueError("Usługa zgłosiła błąd: " + "; ".join(errors)[:500])
    return root


def xml_to_string(element, normalize_gml=False):
    """Serialize a SafeXmlElement subtree without invoking a second XML parser."""

    def qname(expanded, is_attribute=False):
        namespace_uri, local = _split_expanded_name(expanded)
        if normalize_gml and namespace_uri in _GML_NAMESPACES:
            return f"gml:{local}"
        if namespace_uri:
            # Geometry payloads are expected to be GML. Keep unknown names local
            # rather than manufacturing undeclared prefixes.
            return local if not is_attribute else local
        return local

    def render(node, root_node=False):
        name = qname(node.tag)
        attributes = []
        if root_node and normalize_gml:
            attributes.append('xmlns:gml="http://www.opengis.net/gml"')
        for key, value in node.attrib.items():
            attributes.append(f'{qname(key, True)}="{escape(str(value), quote=True)}"')
        attrs = (" " + " ".join(attributes)) if attributes else ""
        content = escape(node.text or "", quote=False)
        for child in node.children:
            content += render(child)
        if not content:
            return f"<{name}{attrs}/>"
        return f"<{name}{attrs}>{content}</{name}>"

    return render(element, root_node=True)


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
