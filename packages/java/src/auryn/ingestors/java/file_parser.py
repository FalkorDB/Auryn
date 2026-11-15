"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned,
no tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic
and takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-java 0.23.5
(see the package's grammar probe), not assumed:

- top level: `package_declaration` (child scoped_identifier/identifier), `import_declaration`
  (scoped_identifier + optional `asterisk`/`static`), and the type declarations
  `class_declaration` / `interface_declaration` / `enum_declaration` / `record_declaration`.
- class: fields `name`, `type_parameters`, `superclass` (single), `interfaces`
  (super_interfaces -> type_list), `body` (class_body); interface uses `extends_interfaces`.
- members: `field_declaration` (modifiers + [type] + repeated [declarator] variable_declarator),
  `method_declaration` ([type]=return, [name], [parameters], optional `throws`, [body]),
  `constructor_declaration` (no return type), nested type declarations.
- modifiers: keyword tokens (public/static/final/abstract/...) are ANONYMOUS children of the
  `modifiers` node; annotations are its named `marker_annotation`/`annotation` children.
- calls: `method_invocation` ([object]?, [name], [arguments]) and `object_creation_expression`
  ([type], [arguments]); `super(...)`/`this(...)` are explicit_constructor_invocation, ignored.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.java.grammar import new_parser
from auryn.ingestors.java.raw_model import (
    RawAnnotation,
    RawCall,
    RawEnumMember,
    RawField,
    RawFile,
    RawImport,
    RawMethod,
    RawParameter,
    RawType,
)

_TYPE_DECL_KINDS = {
    "class_declaration": "Class",
    "interface_declaration": "Interface",
    "enum_declaration": "Enum",
    "record_declaration": "Record",
}
_ACCESS_KEYWORDS = ("public", "private", "protected")


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)
    rf = RawFile(path=path_label)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def field(node, name):
        return node.child_by_field_name(name) if node is not None else None

    def first_type_name(node) -> str | None:
        """The bare simple name a type node denotes: `Base` for `Base`, `Base<T>`
        (generic_type), or `pkg.Base` (scoped_type_identifier). None if it names nothing."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return txt(node)
        if node.type == "generic_type":
            return first_type_name(node.child(0))
        if node.type == "scoped_type_identifier":
            # last segment is the simple name
            last = None
            for c in node.children:
                if c.type == "type_identifier":
                    last = c
            return txt(last) if last is not None else None
        # descend to the first type_identifier we can find
        stack = list(node.children)
        while stack:
            n = stack.pop(0)
            if n.type == "type_identifier":
                return txt(n)
            stack.extend(n.children)
        return None

    def modifiers_of(node) -> tuple[list[str], list[RawAnnotation]]:
        """(keyword modifiers, annotations) from a declaration's `modifiers` child."""
        mods_node = next((c for c in node.children if c.type == "modifiers"), None)
        if mods_node is None:
            return [], []
        keywords: list[str] = []
        annotations: list[RawAnnotation] = []
        for c in mods_node.children:
            if c.type in ("marker_annotation", "annotation"):
                annotations.append(_annotation(c))
            elif not c.is_named:  # public/private/protected/static/final/abstract/...
                keywords.append(c.type)
        return keywords, annotations

    def _annotation(node) -> RawAnnotation:
        name = first_type_name(field(node, "name")) or txt(field(node, "name"))
        args: list[str] = []
        arglist = field(node, "arguments")
        if arglist is not None:
            for a in arglist.named_children:
                args.append(txt(a))
        return RawAnnotation(name=name or "", arguments=args)

    def accessibility(keywords: list[str]) -> str:
        for kw in _ACCESS_KEYWORDS:
            if kw in keywords:
                return kw
        return "package"  # Java's default (package-private)

    def type_parameters(node) -> list[str]:
        tp = field(node, "type_parameters")
        if tp is None:
            return []
        out = []
        for c in tp.named_children:
            if c.type == "type_parameter":
                ident = next((g for g in c.children if g.type == "type_identifier"), None)
                if ident is not None:
                    out.append(txt(ident))
        return out

    def interface_names(super_interfaces_or_extends) -> list[str]:
        if super_interfaces_or_extends is None:
            return []
        type_list = next((c for c in super_interfaces_or_extends.children if c.type == "type_list"), None)
        if type_list is None:
            return []
        out = []
        for c in type_list.named_children:
            nm = first_type_name(c)
            if nm:
                out.append(nm)
        return out

    def parameters(formal_parameters_node) -> list[RawParameter]:
        if formal_parameters_node is None:
            return []
        out: list[RawParameter] = []
        pos = 0
        for c in formal_parameters_node.children:
            if c.type == "formal_parameter":
                name_node = field(c, "name")
                out.append(
                    RawParameter(
                        name=txt(name_node), type=_type_text(field(c, "type")), position=pos,
                        is_final=any(g.type == "final" for g in c.children),
                    )
                )
                pos += 1
            elif c.type == "spread_parameter":  # `T... name`
                type_node = next((g for g in c.children if g.type not in ("...", "variable_declarator")), None)
                declarator = next((g for g in c.children if g.type == "variable_declarator"), None)
                name_node = field(declarator, "name") if declarator is not None else None
                out.append(
                    RawParameter(
                        name=txt(name_node), type=_type_text(type_node), position=pos, is_varargs=True,
                    )
                )
                pos += 1
        return out

    def _type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def collect_calls(body) -> list[RawCall]:
        if body is None:
            return []
        out: list[RawCall] = []
        seen: set = set()
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "method_invocation":
                name_node = field(n, "name")
                obj = field(n, "object")
                recv = None
                if obj is not None and obj.type == "this":
                    recv = "this"
                elif obj is not None and obj.type == "identifier":
                    recv = txt(obj)
                nm = txt(name_node)
                key = (nm, recv, False)
                if nm and key not in seen:
                    seen.add(key)
                    out.append(RawCall(name=nm, receiver=recv, is_new=False))
            elif n.type == "object_creation_expression":
                nm = first_type_name(field(n, "type"))
                key = (nm, None, True)
                if nm and key not in seen:
                    seen.add(key)
                    out.append(RawCall(name=nm, receiver=None, is_new=True))
            stack.extend(n.children)
        return out

    def build_method(node, kind: str) -> RawMethod:
        keywords, annotations = modifiers_of(node)
        throws_node = next((c for c in node.children if c.type == "throws"), None)
        throws = [first_type_name(c) for c in throws_node.named_children] if throws_node is not None else []
        body = field(node, "body")
        ls, le = lines(node)
        return RawMethod(
            name=txt(field(node, "name")),
            kind=kind,
            accessibility=accessibility(keywords),
            modifiers=keywords,
            return_type=_type_text(field(node, "type")) if kind == "ORDINARY" else None,
            parameters=parameters(field(node, "parameters")),
            throws=[t for t in throws if t],
            annotations=annotations,
            calls=collect_calls(body),
            source=txt(node),
            doc=leading_comment(node, source),
            line_start=ls, line_end=le,
        )

    def build_fields(node) -> list[RawField]:
        keywords, annotations = modifiers_of(node)
        type_text = _type_text(field(node, "type"))
        ls, le = lines(node)
        out = []
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = field(declarator, "name")
            out.append(
                RawField(
                    name=txt(name_node), type=type_text, accessibility=accessibility(keywords),
                    modifiers=keywords, annotations=annotations, line_start=ls, line_end=le,
                )
            )
        return out

    def enum_members(enum_body) -> list[RawEnumMember]:
        out = []
        for c in enum_body.children:
            if c.type == "enum_constant":
                arglist = field(c, "arguments")
                args = [txt(a) for a in arglist.named_children] if arglist is not None else []
                out.append(RawEnumMember(name=txt(field(c, "name")), arguments=args))
        return out

    def dispatch_member(member, rt: RawType) -> None:
        if member.type == "field_declaration":
            rt.fields.extend(build_fields(member))
        elif member.type == "method_declaration":
            rt.methods.append(build_method(member, "ORDINARY"))
        elif member.type == "constructor_declaration":
            rt.methods.append(build_method(member, "CONSTRUCTOR"))
        elif member.type == "compact_constructor_declaration":  # record compact ctor
            rt.methods.append(build_method(member, "CONSTRUCTOR"))
        elif member.type in _TYPE_DECL_KINDS:
            rt.nested_types.append(build_type(member, rt.package))

    def build_type(node, package: str) -> RawType:
        kind = _TYPE_DECL_KINDS[node.type]
        keywords, annotations = modifiers_of(node)
        ls, le = lines(node)
        rt = RawType(
            kind=kind, name=txt(field(node, "name")), package=package,
            accessibility=accessibility(keywords), modifiers=keywords,
            type_parameters=type_parameters(node), annotations=annotations,
            source=txt(node), doc=leading_comment(node, source), line_start=ls, line_end=le,
        )

        if kind == "Class":
            rt.superclass = first_type_name(field(node, "superclass"))
            rt.interfaces = interface_names(field(node, "interfaces"))
        elif kind == "Interface":
            ext = next((c for c in node.children if c.type == "extends_interfaces"), None)
            rt.interfaces = interface_names(ext)
        elif kind in ("Enum", "Record"):
            rt.interfaces = interface_names(field(node, "interfaces"))

        if kind == "Record":
            rt.record_components = parameters(field(node, "parameters"))

        body = field(node, "body")
        if body is None:
            return rt

        if body.type == "enum_body":
            rt.enum_members = enum_members(body)
            decls = next((c for c in body.children if c.type == "enum_body_declarations"), None)
            if decls is not None:
                for member in decls.children:
                    dispatch_member(member, rt)
            return rt

        for member in body.children:  # class_body / interface_body
            dispatch_member(member, rt)
        return rt

    for child in tree.root_node.children:
        if child.type == "package_declaration":
            name_node = next((c for c in child.children if c.type in ("scoped_identifier", "identifier")), None)
            rf.package = txt(name_node)
        elif child.type == "import_declaration":
            name_node = next((c for c in child.children if c.type in ("scoped_identifier", "identifier")), None)
            rf.imports.append(
                RawImport(
                    name=txt(name_node),
                    is_static=any(c.type == "static" for c in child.children),
                    is_wildcard=any(c.type == "asterisk" for c in child.children),
                )
            )
        elif child.type in _TYPE_DECL_KINDS:
            rf.types.append(build_type(child, rf.package))

    return rf
