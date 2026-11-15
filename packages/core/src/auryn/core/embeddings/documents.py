"""Assemble the text embedded for a graph node from its stored properties.

Each embeddable node's vector is built from a compact *document*, not its raw source:

- **Callables** (`Function`/`Method`): `doc` + `source` -- the intent (leading comment) plus the
  full implementation including in-body comments. This is the highest-signal document and needs
  no synthesis.
- **Named types** (`Class`/`Struct`/`Interface`/...): `doc` + a synthesized declaration header +
  (when the caller supplies them) member signatures. A type's raw `source` inlines its methods'
  *bodies*, which are already embedded on their own `Method` nodes -- embedding the raw class
  source too would make KNN return a class and each of its methods as near-duplicates. So a type
  gets a summary, never its raw source.
- **Files / modules / packages**: `doc`/header + a symbol inventory (the names of what they
  define). Whole-file text is low-signal and routinely over the model's context window.

The qualified name is always prepended so lexical identity is part of the vector. Documents are
truncated to a character budget -- a coarse proxy for the model's token window (jina-v2-base-code
handles 8192 tokens, so the budget is generous); `name` + `doc` are always kept whole and only
the body is trimmed.
"""

from __future__ import annotations

CALLABLE_LABELS = frozenset({"Function", "Method"})
TYPE_LABELS = frozenset(
    {
        "Class", "Struct", "Interface", "Trait", "Enum", "Record", "Union",
        "AbstractType", "PrimitiveType", "TypeAlias", "Delegate", "DTO",
    }
)
CONTAINER_LABELS = frozenset({"File", "Module", "Package", "Namespace"})

EMBEDDABLE_LABELS = CALLABLE_LABELS | TYPE_LABELS | CONTAINER_LABELS

# Coarse char budget standing in for the model's token window (~4 chars/token, jina = 8192 tok).
MAX_CHARS = 20_000


def is_embeddable(label: str) -> bool:
    return label in EMBEDDABLE_LABELS


def build_document(label: str, properties: dict, *, members: list[str] | None = None) -> str | None:
    """The text to embed for a node, or None if the node carries nothing worth embedding.

    `members` is optional extra context the caller (which has graph access, unlike this pure
    function) can supply: for a type, its members' signature strings; for a container, the names
    of the symbols it defines. Without it, types fall back to a declaration header and containers
    to name+doc only."""
    if label not in EMBEDDABLE_LABELS:
        return None

    name = str(properties.get("qualified_name") or properties.get("name") or "").strip()
    doc = str(properties.get("doc") or "").strip()

    if label in CALLABLE_LABELS:
        body = str(properties.get("source") or "").strip()
    elif label in TYPE_LABELS:
        header = _declaration_header(str(properties.get("source") or ""))
        body = _join(header, _members_block(members))
    else:  # container
        body = _members_block(members)

    document = _assemble(name, doc, body)
    return document or None


def _assemble(name: str, doc: str, body: str, budget: int = MAX_CHARS) -> str:
    """name + doc kept whole; body trimmed to fit the budget."""
    head = _join(name, doc)
    if not body:
        return head[:budget].strip()
    remaining = max(0, budget - len(head) - 1)
    return _join(head, body[:remaining]).strip()


def _declaration_header(source: str) -> str:
    """A type's declaration line(s) without its body: everything up to the opening `{` (C-family,
    Go, ...) or, for brace-less languages (Python `class Foo(Base):`), the first line. Captures the
    name, bases/generics, and kind while excluding the member bodies embedded elsewhere."""
    if not source:
        return ""
    brace = source.find("{")
    head = source[:brace] if brace != -1 else source.split("\n", 1)[0]
    return head.strip()


def _members_block(members: list[str] | None) -> str:
    if not members:
        return ""
    return "\n".join(m.strip() for m in members if m and m.strip())


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)
