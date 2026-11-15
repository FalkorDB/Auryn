"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are
returned, no tree-sitter node crosses the boundary). parse_source(source, path_label) holds
the logic and takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-c 0.24.2
(see the package's grammar probe), not assumed: top-level `preproc_include` (field `path`),
`preproc_def`/`preproc_function_def` (`name`), `type_definition` (`declarator`,`type`),
`struct_specifier`/`union_specifier`/`enum_specifier` (`name`,`body`), `declaration`
(`declarator` -> function_declarator = prototype, else a variable), and `function_definition`
(`declarator`=function_declarator, `type`, `body`). Names sit at the bottom of possibly
pointer/array-wrapped declarators, so `_name_of` descends the `declarator` field to the leaf
`identifier`/`field_identifier`/`type_identifier`."""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.c.grammar import new_parser
from auryn.ingestors.c.raw_model import (
    RawEnum,
    RawField,
    RawFile,
    RawFunction,
    RawGlobal,
    RawInclude,
    RawMacro,
    RawParameter,
    RawRecord,
    RawTypedef,
)

_NAME_LEAVES = {"identifier", "field_identifier", "type_identifier"}
_RECORD_KINDS = {"struct_specifier": "struct", "union_specifier": "union"}
# Wrappers that hold ordinary top-level declarations as children rather than being
# declarations themselves -- header guards / feature #ifs and `extern "C"` blocks. We treat
# their children as if they sat at file scope (recursively, since they nest). Without this a
# guarded header (`#ifndef FOO_H ... #endif`, i.e. almost every real .h) yields nothing.
_TRANSPARENT = {
    "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else", "preproc_elifdef",
    "preproc_elifndef", "linkage_specification",
}


def _iter_decls(node):
    """Yield declaration-level nodes under `node`, flattening preprocessor-conditional and
    linkage wrappers so guarded declarations are seen at file scope."""
    for child in node.children:
        if child.type in _TRANSPARENT:
            yield from _iter_decls(child)
        else:
            yield child


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)
    rf = RawFile(path=path_label)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        # tree-sitter points are 0-based (row, col); expose 1-based inclusive line numbers.
        return node.start_point[0] + 1, node.end_point[0] + 1

    def name_of(decl) -> str:
        """Descend a (possibly pointer/array/function/init/parenthesized) declarator to the
        leaf name identifier."""
        node = decl
        seen = 0
        while node is not None and seen < 64:
            if node.type in _NAME_LEAVES:
                return txt(node)
            inner = node.child_by_field_name("declarator")
            if inner is None:
                # abstract declarator (unnamed param) or an unexpected shape
                return ""
            node = inner
            seen += 1
        return ""

    def name_node_of(decl):
        """Like name_of, but returns the leaf name NODE (or None) so callers can excise it."""
        node = decl
        seen = 0
        while node is not None and seen < 64:
            if node.type in _NAME_LEAVES:
                return node
            node = node.child_by_field_name("declarator")
            seen += 1
        return None

    def written_type(container, name_node) -> str:
        """The type exactly as written for a parameter/field: the whole declaration text minus
        the identifier being declared. This keeps `const`, `*`, `[]`, and qualifiers that live
        in the declarator (not in the `type` field) -- e.g. `const Widget *w` -> `const Widget *`,
        `int buf[10]` -> `int [10]`."""
        a, b = container.start_byte, container.end_byte
        if name_node is not None and a <= name_node.start_byte and name_node.end_byte <= b:
            piece = source[a : name_node.start_byte] + source[name_node.end_byte : b]
        else:
            piece = source[a:b]
        # A field_declaration container carries the trailing `;`; a bitfield carries `: width`.
        text = " ".join(piece.decode("utf-8", "replace").split()).strip()
        text = text.rstrip(";").strip()
        if ":" in text:  # bitfield: keep the type, drop the width spec
            text = text.split(":", 1)[0].strip()
        return text

    def signature_of(node, top_declarator) -> str:
        """The function header exactly as written -- everything from the start of the declaration
        (storage class + return type) through the end of the parameter list `)`, with the body (if
        any) excluded. `top_declarator` ends at the `)` for both prototypes and definitions."""
        if top_declarator is None:
            return ""
        a, b = node.start_byte, top_declarator.end_byte
        return " ".join(source[a:b].decode("utf-8", "replace").split()).strip()

    def find_function_declarator(decl):
        """A function's name lives in a function_declarator, itself possibly wrapped by a
        pointer_declarator (`char *f(void)`). Descend `declarator` fields to find it."""
        node = decl
        seen = 0
        while node is not None and seen < 64:
            if node.type == "function_declarator":
                return node
            node = node.child_by_field_name("declarator")
            seen += 1
        return None

    def is_static(node) -> bool:
        return any(
            c.type == "storage_class_specifier" and txt(c) == "static" for c in node.children
        )

    def type_text(type_node) -> str:
        return " ".join(txt(type_node).split()) if type_node else ""

    def type_ref(type_node) -> str | None:
        """The bare tag/typedef name a type names, if any (for REFERENCES); None for
        primitives like int/char."""
        if type_node is None:
            return None
        if type_node.type in _RECORD_KINDS or type_node.type == "enum_specifier":
            n = type_node.child_by_field_name("name")
            return txt(n) if n else None
        if type_node.type == "type_identifier":
            return txt(type_node)
        return None

    def parameters(func_declarator) -> list[RawParameter]:
        plist = func_declarator.child_by_field_name("parameters")
        out: list[RawParameter] = []
        if plist is None:
            return out
        pos = 0
        for p in plist.children:
            if p.type != "parameter_declaration":
                continue  # skips '(', ')', ',', and variadic '...'
            d = p.child_by_field_name("declarator")
            nnode = name_node_of(d) if d is not None else None
            pname = txt(nnode) if nnode is not None else ""
            t_written = written_type(p, nnode)  # precise: keeps const/*/[]
            if t_written == "void" and d is None:
                continue  # `(void)` is "no parameters", not one void parameter
            out.append(RawParameter(name=pname, type=t_written, position=pos))
            pos += 1
        return out

    def collect_calls(body) -> list[str]:
        names: list[str] = []
        stack = list(body.children)
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "identifier":
                    names.append(txt(fn))
            stack.extend(n.children)
        # de-dup, preserve order
        seen: set[str] = set()
        return [x for x in names if not (x in seen or seen.add(x))]

    def record_from_specifier(spec, outer=None) -> RawRecord | None:
        name_node = spec.child_by_field_name("name")
        body = spec.child_by_field_name("body")
        if name_node is None:
            return None  # anonymous struct/union -- no stable identity, skip
        ls, le = lines(spec)
        # A leading comment sits above the top-level struct specifier, OR -- when the record is
        # defined inline in a typedef/declaration -- above that wrapping node (`outer`).
        doc = leading_comment(spec, source) or (leading_comment(outer, source) if outer is not None else "")
        rec = RawRecord(kind=_RECORD_KINDS[spec.type], name=txt(name_node), is_definition=body is not None,
                        source=txt(spec), doc=doc, line_start=ls, line_end=le)
        if body is not None:
            for fd in body.children:
                if fd.type != "field_declaration":
                    continue
                ftype = fd.child_by_field_name("type")
                fdecl = fd.child_by_field_name("declarator")
                nnode = name_node_of(fdecl) if fdecl is not None else None
                fname = txt(nnode) if nnode is not None else ""
                if fname:
                    fls, fle = lines(fd)
                    rec.fields.append(RawField(name=fname, type=written_type(fd, nnode),  # precise: keeps *, [], const
                                               type_ref=type_ref(ftype), line_start=fls, line_end=fle))
        return rec

    def enum_from_specifier(spec, outer=None) -> RawEnum | None:
        name_node = spec.child_by_field_name("name")
        body = spec.child_by_field_name("body")
        if name_node is None:
            return None
        ls, le = lines(spec)
        doc = leading_comment(spec, source) or (leading_comment(outer, source) if outer is not None else "")
        en = RawEnum(name=txt(name_node), is_definition=body is not None, source=txt(spec), doc=doc, line_start=ls, line_end=le)
        if body is not None:
            for e in body.children:
                if e.type == "enumerator":
                    en.members.append(txt(e.child_by_field_name("name")))
        return en

    def emit_inline_type(type_node, outer=None) -> None:
        """A struct/union/enum specifier used inline in a typedef/declaration can itself
        carry a body -- that's a definition of the tag, emitted alongside. `outer` is the
        wrapping type_definition/declaration, so a doc comment above it is picked up."""
        if type_node is None:
            return
        if type_node.type in _RECORD_KINDS:
            rec = record_from_specifier(type_node, outer)
            if rec is not None and rec.is_definition:
                rf.records.append(rec)
        elif type_node.type == "enum_specifier":
            en = enum_from_specifier(type_node, outer)
            if en is not None and en.is_definition:
                rf.enums.append(en)

    for child in _iter_decls(tree.root_node):
        kind = child.type
        if kind == "preproc_include":
            p = child.child_by_field_name("path")
            if p is not None:
                is_system = p.type == "system_lib_string"
                rf.includes.append(RawInclude(path=txt(p).strip('<>"'), is_system=is_system))
        elif kind == "preproc_def":
            ls, le = lines(child)
            rf.macros.append(RawMacro(name=txt(child.child_by_field_name("name")), is_function_like=False,
                                      source=txt(child).rstrip(), line_start=ls, line_end=le))
        elif kind == "preproc_function_def":
            ls, le = lines(child)
            rf.macros.append(RawMacro(name=txt(child.child_by_field_name("name")), is_function_like=True,
                                      source=txt(child).rstrip(), line_start=ls, line_end=le))
        elif kind == "type_definition":
            type_node = child.child_by_field_name("type")
            decl = child.child_by_field_name("declarator")
            alias = name_of(decl)
            if alias:
                ls, le = lines(child)
                rf.typedefs.append(RawTypedef(name=alias, underlying=type_text(type_node),
                                              underlying_ref=type_ref(type_node), source=txt(child),
                                              doc=leading_comment(child, source),
                                              line_start=ls, line_end=le))
            emit_inline_type(type_node, child)
        elif kind in _RECORD_KINDS:
            rec = record_from_specifier(child)
            if rec is not None:
                rf.records.append(rec)
        elif kind == "enum_specifier":
            en = enum_from_specifier(child)
            if en is not None:
                rf.enums.append(en)
        elif kind == "declaration":
            decl = child.child_by_field_name("declarator")
            fdecl = find_function_declarator(decl) if decl is not None else None
            if fdecl is not None:
                name = name_of(fdecl.child_by_field_name("declarator"))
                if name:
                    ls, le = lines(child)
                    rf.functions.append(
                        RawFunction(
                            name=name,
                            return_type=type_text(child.child_by_field_name("type")),
                            params=parameters(fdecl),
                            is_static=is_static(child),
                            is_definition=False,
                            signature=signature_of(child, decl),
                            source=txt(child), doc=leading_comment(child, source),
                            line_start=ls, line_end=le,
                        )
                    )
            else:
                name = name_of(decl) if decl is not None else ""
                if name:
                    ls, le = lines(child)
                    rf.globals.append(
                        RawGlobal(name=name, type=type_text(child.child_by_field_name("type")),
                                  is_static=is_static(child), source=txt(child), line_start=ls, line_end=le)
                    )
            emit_inline_type(child.child_by_field_name("type"), child)
        elif kind == "function_definition":
            decl = child.child_by_field_name("declarator")
            fdecl = find_function_declarator(decl)
            if fdecl is not None:
                name = name_of(fdecl.child_by_field_name("declarator"))
                if name:
                    body = child.child_by_field_name("body")
                    ls, le = lines(child)
                    rf.functions.append(
                        RawFunction(
                            name=name,
                            return_type=type_text(child.child_by_field_name("type")),
                            params=parameters(fdecl),
                            is_static=is_static(child),
                            is_definition=True,
                            calls=collect_calls(body) if body is not None else [],
                            signature=signature_of(child, decl),
                            source=txt(child), doc=leading_comment(child, source),
                            line_start=ls, line_end=le,
                        )
                    )

    return rf
