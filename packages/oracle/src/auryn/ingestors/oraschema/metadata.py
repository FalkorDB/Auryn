"""Read the two UTF-16 metadata files a Redgate "Schema Compare for Oracle"
scripts-folder export drops next to the object DDL:

  DatabaseInformation.xml   <ScriptsFolderInformation><DatabaseVersion>NineteenC</...>
  <SCHEMA>/SchemaInformation.xml   <SchemaInformation><Name>MYSCHEMA</Name></...>

Both are UTF-16 with an XML encoding declaration; ElementTree's parser (expat) honours
that declaration/BOM when handed the raw bytes, so no manual decode is needed. Tag
lookups are done by local name so a namespaced variant of the same export still reads.
Everything here degrades gracefully -- a missing or unreadable metadata file yields
None, never an error, since the object DDL is the real payload.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger("auryn.oraschema")

# Redgate spells the Oracle release as an English word (the DatabaseVersion enum);
# map the ones we've seen to the conventional short form. Unknown values pass through
# verbatim rather than being dropped, so a newer release is still recorded.
_VERSION_MAP = {
    "TenG": "10g",
    "ElevenG": "11g",
    "TwelveC": "12c",
    "EighteenC": "18c",
    "NineteenC": "19c",
    "TwentyOneC": "21c",
    "TwentyThreeAI": "23ai",
    "TwentyThreeC": "23c",
}


def _localname(tag: str) -> str:
    """Strip an XML namespace ("{ns}Name" -> "Name") so lookups work namespaced or not."""
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str | None:
    """First element (root or descendant) whose local tag == name, its stripped text."""
    for element in root.iter():
        if _localname(element.tag) == name:
            text = (element.text or "").strip()
            if text:
                return text
    return None


def _parse_xml(path: Path) -> ET.Element | None:
    try:
        return ET.fromstring(path.read_bytes())  # expat picks up the utf-16 declaration/BOM
    except (OSError, ET.ParseError) as error:
        logger.warning("Could not read metadata %s: %s", path.name, error)
        return None


def read_database_version(root_dir: Path) -> str | None:
    """DatabaseInformation.xml -> a short Oracle release string (e.g. "19c"), or None."""
    path = root_dir / "DatabaseInformation.xml"
    if not path.exists():
        return None
    root = _parse_xml(path)
    if root is None:
        return None
    raw = _find_text(root, "DatabaseVersion")
    if raw is None:
        return None
    return _VERSION_MAP.get(raw, raw)


def read_schema_name(schema_dir: Path) -> str | None:
    """<schema>/SchemaInformation.xml -> the schema's <Name>, or None if absent."""
    path = schema_dir / "SchemaInformation.xml"
    if not path.exists():
        return None
    root = _parse_xml(path)
    if root is None:
        return None
    return _find_text(root, "Name")
