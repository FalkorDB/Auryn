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

    def first_type_name_node(node):
        """The NODE of the bare simple name a type denotes: `Base` for `Base`, `Base<T>` (generic_type),
        or the FINAL segment `Base` for `pkg.Base` (scoped_type_identifier). None if it names nothing. The
        node (not just its text) is what a constructor call site needs for its line -- SCIP anchors the
        `<init>` occurrence at this final identifier, which for a multiline `new pkg.\n Base()` sits on a
        different line than the scoped type's start."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return node
        if node.type == "generic_type":
            return first_type_name_node(node.child(0))
        if node.type == "scoped_type_identifier":
            last = None  # last segment is the simple name
            for c in node.children:
                if c.type == "type_identifier":
                    last = c
            return last
        stack = list(node.children)  # descend to the first type_identifier we can find
        while stack:
            n = stack.pop(0)
            if n.type == "type_identifier":
                return n
            stack.extend(n.children)
        return None

    def first_type_name(node) -> str | None:
        """The bare simple name a type node denotes (see first_type_name_node). None if it names nothing."""
        n = first_type_name_node(node)
        return txt(n) if n is not None else None

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

    def local_types(body, params: list[RawParameter]) -> dict[str, str]:
        """name -> declared type for the method's parameters and every typed local it declares
        (`T x = ...`, `for (T x : ...)`, try-with-resources). Method-wide, not block-scoped: a
        name re-declared with another type in a sibling block keeps the first -- rare, and still a
        typed receiver. `var` declarations are left out (their type isn't written)."""
        out = {p.name: p.type for p in params if p.name and p.type}
        if body is None:
            return out
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("local_variable_declaration", "enhanced_for_statement", "resource"):
                type_text = _type_text(field(n, "type"))
                if type_text and type_text != "var":
                    names = [field(d, "name") for d in n.children if d.type == "variable_declarator"]
                    if n.type != "local_variable_declaration":
                        names = [field(n, "name")]
                    for nm in names:
                        if nm is not None:
                            out.setdefault(txt(nm), type_text)
            stack.extend(n.children)
        return out

    def arg_info(node, locals_: dict[str, str]) -> tuple[str | None, str | None]:
        """(static type, field-candidate identifier) of one argument expression -- only what is
        visible without type inference; anything else is (None, None) = fits any parameter."""
        t = node.type
        if t in ("string_literal", "text_block"):
            return "String", None
        if t == "character_literal":
            return "char", None
        if t in ("true", "false"):
            return "boolean", None
        if t in ("decimal_integer_literal", "hex_integer_literal", "octal_integer_literal", "binary_integer_literal"):
            return ("long" if txt(node)[-1:] in "lL" else "int"), None
        if t in ("decimal_floating_point_literal", "hex_floating_point_literal"):
            return ("float" if txt(node)[-1:] in "fF" else "double"), None
        if t == "class_literal":
            return "Class", None
        if t == "object_creation_expression":
            name_node = first_type_name_node(field(node, "type"))
            return (txt(name_node) if name_node is not None else None), None
        if t == "cast_expression":
            return (_type_text(field(node, "type")) or None), None
        if t == "identifier":
            name = txt(node)
            return (locals_[name], None) if name in locals_ else (None, name)
        return None, None

    def receiver_chain(obj, locals_: dict[str, str]):
        """(base_kind, base, steps) for a receiver built by `new T()` / a name / `this` followed by
        up to 8 method calls; None for anything else (a field access, an array, a lambda...)."""
        steps = []
        while obj is not None and len(steps) <= 8:
            if obj.type == "parenthesized_expression":
                obj = next(iter(obj.named_children), None)
                continue
            if obj.type == "method_invocation":
                args_node = field(obj, "arguments")
                steps.append((txt(field(obj, "name")), len(args_node.named_children) if args_node is not None else 0))
                obj = field(obj, "object")
                if obj is None:
                    return None  # a bare call starts the chain: its owner isn't named here
                continue
            steps.reverse()
            if obj.type == "object_creation_expression":
                name_node = first_type_name_node(field(obj, "type"))
                return ("new", txt(name_node), steps) if name_node is not None else None
            if obj.type == "identifier":
                return ("var", txt(obj), steps)
            if obj.type == "this":
                return ("this", "", steps)
            return None
        return None

    def chain_of(obj, locals_: dict[str, str]):
        chain = receiver_chain(obj, locals_)
        if chain is not None and chain[0] == "var" and chain[1] in locals_:
            return ("var", chain[1], chain[2], locals_[chain[1]])
        return chain

    def collect_calls(body, params: list[RawParameter] | None = None) -> list[RawCall]:
        if body is None:
            return []
        locals_ = local_types(body, params or [])
        out: list[RawCall] = []
        seen: set = set()
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "method_invocation":
                name_node = field(n, "name")
                obj = field(n, "object")
                recv = None
                chained = False
                if obj is not None and obj.type == "this":
                    recv = "this"
                elif obj is not None and obj.type == "identifier":
                    recv = txt(obj)
                elif obj is not None:
                    chained = True  # a chain / field access / super: the receiver can't be named
                nm = txt(name_node)
                line = (name_node.start_point[0] + 1) if name_node is not None else 0
                args_node = field(n, "arguments")
                args = [arg_info(a, locals_) for a in args_node.named_children] if args_node is not None else []
                # the line is part of the dedup key: the deep pass's line-precise gate needs EVERY call
                # site's line, so `store.put()` on two lines keeps both (a name-only key would drop one).
                # The argument count is too: two overloads called on one line are two calls.
                key = (nm, recv, False, line, len(args))
                if nm and key not in seen:
                    seen.add(key)
                    out.append(RawCall(
                        name=nm, receiver=recv, is_new=False, line=line,
                        receiver_type=locals_.get(recv) if recv not in (None, "this") else None,
                        chained=chained, arg_count=len(args),
                        receiver_chain=chain_of(obj, locals_) if chained else None,
                        arg_types=[a[0] for a in args], arg_names=[a[1] for a in args],
                    ))
            elif n.type == "object_creation_expression":
                name_node = first_type_name_node(field(n, "type"))  # the FINAL type identifier, not the
                nm = txt(name_node) if name_node is not None else None  # scoped type's start -- SCIP anchors
                line = (name_node.start_point[0] + 1) if name_node is not None else 0  # <init> here
                key = (nm, None, True, line)
                if nm and key not in seen:
                    seen.add(key)
                    out.append(RawCall(name=nm, receiver=None, is_new=True, line=line))
            stack.extend(n.children)
        return out

    def build_method(node, kind: str) -> RawMethod:
        keywords, annotations = modifiers_of(node)
        throws_node = next((c for c in node.children if c.type == "throws"), None)
        throws = [first_type_name(c) for c in throws_node.named_children] if throws_node is not None else []
        body = field(node, "body")
        params = parameters(field(node, "parameters"))
        ls, le = lines(node)
        return RawMethod(
            name=txt(field(node, "name")),
            kind=kind,
            accessibility=accessibility(keywords),
            modifiers=keywords,
            return_type=_type_text(field(node, "type")) if kind == "ORDINARY" else None,
            parameters=params,
            throws=[t for t in throws if t],
            annotations=annotations,
            calls=collect_calls(body, params),
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
