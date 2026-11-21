"""A tiny, dependency-free reader for the subset of SCIP (Sourcegraph Code Intelligence Protocol,
https://github.com/sourcegraph/scip) that the deep passes need: per document, every occurrence's
symbol, its start line, and whether that occurrence is the symbol's definition.

SCIP indexers (`rust-analyzer scip`, `scip-typescript index`, scip-java/scip-go, ...) all emit an
`index.scip` protobuf (there is no JSON form) in the SAME wire format, and the repo deliberately carries
no protobuf runtime (lean-install ethos -- see why pyright was dropped). SCIP's wire schema for the
handful of fields we read is small and stable, so we decode the varint / length-delimited wire format by
hand rather than taking a `protobuf` dependency. Field numbers are from scip.proto:

    Index.documents            = 2   (repeated Document)
    Document.relative_path     = 1   (string)
    Document.occurrences       = 2   (repeated Occurrence)
    Occurrence.range             = 1   (DEPRECATED repeated int32 -- [line, startCol, endCol] or
                                         [startLine, startCol, endLine, endCol]; 0-based)
    Occurrence.symbol            = 2   (string)
    Occurrence.symbol_roles      = 3   (int32 bitmask; Definition = 0x1)
    Occurrence.single_line_range = 8   (SingleLineRange{line=1, start_character=2, end_character=3})
    Occurrence.multi_line_range  = 9   (MultiLineRange{start_line=1, start_character=2, end_line=3, ...})

Newer indexers (scip-go, and the scip-code/scip schema generally) emit the TYPED `single_line_range`(8) /
`multi_line_range`(9) instead of the deprecated packed `range`(1); rust-analyzer / scip-typescript still
emit `range`(1). We read whichever is present (typed takes precedence, per the proto) -- only the start
line is needed for the containment join. Everything else in the index (symbols, relationships,
diagnostics, external_symbols) is ignored -- the deep passes derive calls from occurrence containment,
not from the symbol table (see core.scip_resolve)."""

from __future__ import annotations

from dataclasses import dataclass

_WIRE_VARINT = 0
_WIRE_LEN = 2
_WIRE_I64 = 1
_WIRE_I32 = 5

SYMBOL_ROLE_DEFINITION = 0x1


@dataclass(frozen=True)
class Occurrence:
    symbol: str
    start_line: int  # 0-based (the SCIP range's first element)
    is_definition: bool


@dataclass(frozen=True)
class Document:
    relative_path: str
    occurrences: tuple[Occurrence, ...]


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _fields(data: bytes):
    """Yield (field_number, wire_type, payload) over one protobuf message. For LEN fields the payload
    is the raw bytes slice; for VARINT it's the int; fixed-width fields are returned as raw bytes."""
    i, n = 0, len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        field_num, wire = tag >> 3, tag & 0x7
        if wire == _WIRE_VARINT:
            value, i = _read_varint(data, i)
            yield field_num, wire, value
        elif wire == _WIRE_LEN:
            length, i = _read_varint(data, i)
            yield field_num, wire, data[i : i + length]
            i += length
        elif wire == _WIRE_I64:
            yield field_num, wire, data[i : i + 8]
            i += 8
        elif wire == _WIRE_I32:
            yield field_num, wire, data[i : i + 4]
            i += 4
        else:  # SGROUP/EGROUP (3/4) never appear in SCIP
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _packed_varints(data: bytes) -> list[int]:
    out, i, n = [], 0, len(data)
    while i < n:
        value, i = _read_varint(data, i)
        out.append(value)
    return out


def _sub_field1_varint(data: bytes) -> int:
    """The first varint field (field 1) of a sub-message -- the start line of a SingleLineRange (`line`)
    or MultiLineRange (`start_line`)."""
    for field_num, wire, value in _fields(data):
        if field_num == 1 and wire == _WIRE_VARINT:
            return value
    return -1


def _parse_occurrence(data: bytes) -> Occurrence | None:
    symbol = ""
    packed_line = -1  # deprecated range(1)
    typed_line = -1   # single_line_range(8) / multi_line_range(9); takes precedence per the proto
    roles = 0
    for field_num, wire, value in _fields(data):
        if field_num == 1:  # deprecated packed range: [line, startCol, endCol(, endLine, endCol)]
            if wire == _WIRE_LEN:  # packed (proto3 default)
                ints = _packed_varints(value)
                if ints:
                    packed_line = ints[0]
            elif wire == _WIRE_VARINT and packed_line < 0:  # non-packed: first entry is the start line
                packed_line = value
        elif field_num in (8, 9) and wire == _WIRE_LEN:  # single_line_range / multi_line_range
            typed_line = _sub_field1_varint(value)
        elif field_num == 2 and wire == _WIRE_LEN:  # symbol
            symbol = value.decode("utf-8", "replace")
        elif field_num == 3 and wire == _WIRE_VARINT:  # symbol_roles
            roles = value
    start_line = typed_line if typed_line >= 0 else packed_line
    if not symbol or start_line < 0:
        return None
    return Occurrence(symbol=symbol, start_line=start_line, is_definition=bool(roles & SYMBOL_ROLE_DEFINITION))


def _parse_document(data: bytes) -> Document:
    relative_path = ""
    occurrences: list[Occurrence] = []
    for field_num, wire, value in _fields(data):
        if field_num == 1 and wire == _WIRE_LEN:  # relative_path
            relative_path = value.decode("utf-8", "replace")
        elif field_num == 2 and wire == _WIRE_LEN:  # occurrences
            occ = _parse_occurrence(value)
            if occ is not None:
                occurrences.append(occ)
    return Document(relative_path=relative_path, occurrences=tuple(occurrences))


def parse_index(data: bytes) -> list[Document]:
    """Decode an `index.scip` byte string into its documents (relative_path + occurrences)."""
    documents: list[Document] = []
    for field_num, wire, value in _fields(data):
        if field_num == 2 and wire == _WIRE_LEN:  # Index.documents
            documents.append(_parse_document(value))
    return documents
