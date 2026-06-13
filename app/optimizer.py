"""
TinyAnim — Core optimization engine.

Two pure-Python optimizers with **zero external API cost** and no heavy
graphics dependencies:

  * ``LottieOptimizer``  — structural compression of Lottie (.json) animations.
  * ``SVGOptimizer``     — DOM-level + textual cleanup of SVG vector files.

Both are deterministic, side-effect free, and operate entirely in memory on the
bytes you hand them. They never change the *visual* output of a file; they only
remove information a renderer does not need (editor metadata, layer names,
excess float precision, whitespace, unreferenced ids …).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Set


# --------------------------------------------------------------------------- #
#  Lottie (JSON) optimizer
# --------------------------------------------------------------------------- #
class LottieOptimizer:
    """Losslessly-looking compressor for Lottie / Bodymovin JSON.

    Strategy
    --------
    1. Recursively round every floating point value (coordinates, timing,
       bezier handles …) to ``precision`` decimals. This is by far the biggest
       win — After Effects exports values like ``-12.34567890123``.
    2. Strip metadata keys that only matter inside an authoring tool and are
       ignored by every player: ``nm`` (layer/shape name), ``mn`` (match name),
       ``cl`` (css class) and the top-level ``meta`` block.
    3. Re-serialize with the most compact JSON separators (no spaces).
    """

    #: keys that are pure authoring metadata and safe to drop everywhere.
    META_KEYS = frozenset({"nm", "mn", "cl"})

    def __init__(self, precision: int = 3) -> None:
        self.precision = precision

    def optimize(self, data: bytes) -> bytes:
        obj = json.loads(data.decode("utf-8"))

        # The top-level ``meta`` object holds author / generator strings only.
        if isinstance(obj, dict):
            obj.pop("meta", None)

        cleaned = self._clean(obj)
        return json.dumps(
            cleaned, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    # -- internal helpers --------------------------------------------------- #
    def _round(self, value: float) -> Any:
        rounded = round(value, self.precision)
        # Collapse integral floats (``1.0`` -> ``1``) to shave more bytes.
        if rounded == int(rounded):
            return int(rounded)
        return rounded

    def _clean(self, node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, val in node.items():
                if key in self.META_KEYS:
                    continue
                out[key] = self._clean(val)
            return out
        if isinstance(node, list):
            return [self._clean(item) for item in node]
        # ``bool`` is a subclass of ``int`` — leave it untouched.
        if isinstance(node, float):
            return self._round(node)
        return node


# --------------------------------------------------------------------------- #
#  SVG optimizer
# --------------------------------------------------------------------------- #
class SVGOptimizer:
    """A ``scour``-style SVG cleaner implemented in pure Python (stdlib only).

    Pipeline
    --------
    1. Strip ``<?xml?>`` declaration, ``<!DOCTYPE>`` (also closes an XXE vector)
       and comments.
    2. Parse the document and drop:
         * editor-only elements (``metadata``, ``title``, ``desc``,
           ``sodipodi:namedview`` …)
         * editor-only attributes (anything in the inkscape / sodipodi / sketch
           / illustrator / figma namespaces, ``xml:space``, ``data-name`` …)
         * ``id`` attributes that are never referenced (``url(#id)`` / ``#id``).
    3. Round numbers, minify path data and squeeze inter-tag whitespace.
    """

    #: substrings that identify an authoring-tool namespace URI.
    _EDITOR_NS_HINTS = (
        "inkscape",
        "sodipodi",
        "sketch",
        "bohemiancoding",
        "vectornator",
        "figma",
        "adobe",
        "illustrator",
    )

    #: local element names that carry no rendering information.
    _DROP_ELEMENTS = frozenset({"metadata", "title", "desc", "namedview"})

    _XML_NS = "http://www.w3.org/XML/1998/namespace"  # for xml:space etc.
    _SVG_NS = "http://www.w3.org/2000/svg"
    _XLINK_NS = "http://www.w3.org/1999/xlink"

    def __init__(self, precision: int = 2) -> None:
        self.precision = precision

    # -- public API --------------------------------------------------------- #
    def optimize(self, data: bytes) -> bytes:
        text = data.decode("utf-8", errors="replace")

        # Remove the prolog up front: cheaper than walking it, and dropping the
        # DOCTYPE neutralizes classic billion-laughs / external-entity attacks.
        text = re.sub(r"<\?xml.*?\?>", "", text, flags=re.DOTALL)
        text = re.sub(r"<!DOCTYPE.*?>", "", text, flags=re.DOTALL)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

        root = ET.fromstring(text)

        keep_ids = self._has_style(root)  # CSS may reference ids/classes.
        self._strip_tree(root)
        if not keep_ids:
            referenced = self._collect_refs(root)
            self._strip_ids(root, referenced)

        ET.register_namespace("", self._SVG_NS)
        ET.register_namespace("xlink", self._XLINK_NS)
        out = ET.tostring(root, encoding="unicode")

        return self._minify(out).encode("utf-8")

    # -- tree walking ------------------------------------------------------- #
    @staticmethod
    def _localname(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    @staticmethod
    def _namespace(tag: str) -> str:
        if isinstance(tag, str) and tag.startswith("{"):
            return tag[1:].split("}", 1)[0]
        return ""

    def _is_editor_ns(self, uri: str) -> bool:
        low = uri.lower()
        return any(hint in low for hint in self._EDITOR_NS_HINTS)

    def _drop_element(self, elem: ET.Element) -> bool:
        if not isinstance(elem.tag, str):  # comments / PIs (defensive)
            return True
        if self._is_editor_ns(self._namespace(elem.tag)):
            return True
        return self._localname(elem.tag) in self._DROP_ELEMENTS

    def _drop_attr(self, key: str) -> bool:
        ns = self._namespace(key)
        if ns and (self._is_editor_ns(ns) or ns == self._XML_NS):
            return True
        return self._localname(key) in {"data-name"}

    def _has_style(self, elem: ET.Element) -> bool:
        for node in elem.iter():
            if self._localname(node.tag) == "style":
                return True
        return False

    def _strip_tree(self, elem: ET.Element) -> None:
        for key in list(elem.attrib):
            if self._drop_attr(key):
                del elem.attrib[key]
        for child in list(elem):
            if self._drop_element(child):
                elem.remove(child)
            else:
                self._strip_tree(child)

    def _collect_refs(self, root: ET.Element) -> Set[str]:
        refs: Set[str] = set()
        url_re = re.compile(r"url\(\s*#([^)\s]+)\s*\)")
        for elem in root.iter():
            for key, val in elem.attrib.items():
                if not val:
                    continue
                refs.update(url_re.findall(val))
                if self._localname(key) == "href" and val.startswith("#"):
                    refs.add(val[1:])
        return refs

    def _strip_ids(self, root: ET.Element, referenced: Set[str]) -> None:
        for elem in root.iter():
            if "id" in elem.attrib and elem.attrib["id"] not in referenced:
                del elem.attrib["id"]

    # -- textual minification ---------------------------------------------- #
    def _format_number(self, value: float) -> str:
        rounded = round(value, self.precision)
        text = ("%f" % rounded).rstrip("0").rstrip(".")
        if text.startswith("0.") and len(text) > 2:
            text = text[1:]            # 0.5  -> .5
        elif text.startswith("-0.") and len(text) > 3:
            text = "-" + text[2:]      # -0.5 -> -.5
        return text if text not in ("", "-") else "0"

    def _round_numbers(self, text: str) -> str:
        pattern = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")
        return pattern.sub(lambda m: self._format_number(float(m.group(0))), text)

    def _clean_path_data(self, text: str) -> str:
        def repl(match: "re.Match[str]") -> str:
            d = match.group(2)
            d = re.sub(r"\s+", " ", d).strip()
            d = re.sub(r"\s*,\s*", ",", d)
            d = re.sub(r"\s+([a-zA-Z])", r"\1", d)
            d = re.sub(r"([a-zA-Z])\s+", r"\1", d)
            return f'{match.group(1)}="{d}"'

        return re.sub(r'\b(d|points)="([^"]*)"', repl, text)

    def _minify(self, text: str) -> str:
        text = self._round_numbers(text)
        text = self._clean_path_data(text)
        text = re.sub(r">\s+<", "><", text)
        return text.strip()


# --------------------------------------------------------------------------- #
#  Dispatch helper
# --------------------------------------------------------------------------- #
_LOTTIE = LottieOptimizer()
_SVG = SVGOptimizer()


def optimize_bytes(file_type: str, data: bytes) -> bytes:
    """Optimize ``data`` according to ``file_type`` ("lottie" | "svg")."""
    if file_type == "lottie":
        return _LOTTIE.optimize(data)
    if file_type == "svg":
        return _SVG.optimize(data)
    raise ValueError(f"Unsupported file type: {file_type!r}")
