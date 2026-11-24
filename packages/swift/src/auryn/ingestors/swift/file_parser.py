"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared state
-- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned, no
tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-swift 0.7.3
(see this package's grammar probe), not assumed. The grammar has a few traps worth stating:

- class/struct/enum/extension are ALL `class_declaration`, discriminated by a `declaration_kind`
  field ("class"/"struct"/"enum"/"extension"); `protocol` is its own `protocol_declaration`.
  A class/struct/enum/protocol names its type via the `name` field (a `type_identifier`); an
  `extension`'s `name` field is a `user_type` (the type it extends).
- inheritance (`: A, B, C`) is a run of `inheritance_specifier` children (NOT a field), each with
  an `inherits_from` field = `user_type`. The class-vs-protocol split is deferred to resolution.
- `function_declaration` / `parameter` REUSE the field name `name`: it tags both the identifier
  (the func/param name, a `simple_identifier`) AND the type node (return type / param type). We
  disambiguate by node type -- the `simple_identifier` is the name, a non-identifier is the type.
- `property_declaration`: `name` field = a `pattern`; the type is a `type_annotation` child (no
  field); a `computed_value` field marks a computed property; a `value_binding_pattern` says
  var/let. `init_declaration` is a constructor (`name` field = the `init` keyword token).
- `enum_entry` can declare several cases at once (`case a, b, c` -> repeated `name` fields), with
  optional `data_contents` (associated values) / `raw_value` attaching to the preceding name.
- calls: `call_expression` whose callee is a `simple_identifier` (bare `foo()` / construction
  `Foo(...)`) or a `navigation_expression` (`x.m()` / `self.m()` / `Type.m()`), the latter split
  into a `target` and a `suffix` (`navigation_suffix` -> the member `simple_identifier`).
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.swift.grammar import new_parser
from auryn.ingestors.swift.raw_model import (
    RawCall,
    RawEnumMember,
    RawExtension,
    RawFile,
    RawFunction,
    RawImport,
    RawParameter,
    RawProperty,
    RawType,
)

_TYPE_KIND = {"class": "Class", "struct": "Struct", "enum": "Enum"}
_ACCESS_LEVELS = frozenset({"open", "public", "internal", "fileprivate", "private"})


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def field_children(node, name: str) -> list:
        return [node.child(i) for i in range(node.child_count) if node.field_name_for_child(i) == name]

    def simple_type_name(node) -> str | None:
        """The bare base name a type node denotes: `Base` for `Base`, `Base<T>` (user_type +
        type_arguments), `Base?` (optional_type), `[Base]` (array_type). None if it names none."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return txt(node)
        if node.type == "user_type":
            ti = next((c for c in node.children if c.type == "type_identifier"), None)
            if ti is not None:
                return txt(ti)
            subs = [c for c in node.children if c.type == "user_type"]
            if subs:
                return simple_type_name(subs[-1])
        stack = list(node.children)
        while stack:
            n = stack.pop(0)
            if n.type == "type_identifier":
                return txt(n)
            stack.extend(n.children)
        return None

    def type_parameters(node) -> list[str]:
        tp = next((c for c in node.children if c.type == "type_parameters"), None)
        if tp is None:
            return []
        out: list[str] = []
        for c in tp.named_children:
            if c.type == "type_identifier":
                out.append(txt(c))
            else:
                ti = next((g for g in c.children if g.type == "type_identifier"), None)
                if ti is not None:
                    out.append(txt(ti))
        return out

    def inherited_names(node) -> list[str]:
        out: list[str] = []
        for spec in node.children:
            if spec.type != "inheritance_specifier":
                continue
            nm = simple_type_name(spec.child_by_field_name("inherits_from"))
            if nm:
                out.append(nm)
        return out

    def _attr_name(attr_node) -> str:
        return txt(attr_node).lstrip("@").split("(")[0].strip()

    def modifiers_of(node) -> tuple[list[str], list[str], str, bool]:
        """(keyword modifiers, attribute names, accessibility, is_static) from a `modifiers`
        child. accessibility defaults to Swift's implicit `internal`."""
        mods_node = next((c for c in node.children if c.type == "modifiers"), None)
        if mods_node is None:
            return [], [], "internal", False
        keywords: list[str] = []
        attributes: list[str] = []
        for c in mods_node.children:
            if c.type == "attribute":
                attributes.append(_attr_name(c))
            elif c.is_named:
                keywords.append(txt(c))
        access = "internal"
        for k in keywords:
            base = k.split("(")[0]
            if base in _ACCESS_LEVELS:
                access = base
                break
        is_static = any(k in ("static", "class") for k in keywords)
        return keywords, attributes, access, is_static

    def _receiver_key(target) -> str | None:
        if target is None:
            return None
        t = target.type
        if t == "self_expression":
            return "self"
        if t == "simple_identifier":
            return txt(target)
        if t == "navigation_expression":
            suffix = target.child_by_field_name("suffix")
            if suffix is not None:
                sid = next((c for c in suffix.children if c.type == "simple_identifier"), None)
                return txt(sid) if sid is not None else None
        return None

    def _extract_callee(call_node) -> RawCall | None:
        callee = next((c for c in call_node.named_children if c.type != "call_suffix"), None)
        if callee is None:
            return None
        if callee.type == "simple_identifier":  # bare `foo()` or construction `Foo(...)`
            return RawCall(name=txt(callee), receiver=None)
        if callee.type == "navigation_expression":
            suffix = callee.child_by_field_name("suffix")
            if suffix is None:
                return None
            member = next((c for c in suffix.children if c.type == "simple_identifier"), None)
            if member is None:
                return None
            return RawCall(name=txt(member), receiver=_receiver_key(callee.child_by_field_name("target")))
        return None

    def collect_calls(body) -> list[RawCall]:
        if body is None:
            return []
        out: list[RawCall] = []
        seen: set = set()
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                call = _extract_callee(n)
                if call is not None and call.name:
                    key = (call.name, call.receiver)
                    if key not in seen:
                        seen.add(key)
                        out.append(call)
            stack.extend(n.children)
        return out

    def parameters(node) -> list[RawParameter]:
        out: list[RawParameter] = []
        pos = 0
        for c in node.children:
            if c.type != "parameter":
                continue
            name_children = field_children(c, "name")
            internal = next((txt(n) for n in name_children if n.type == "simple_identifier"), "")
            type_node = next((n for n in name_children if n.type != "simple_identifier"), None)
            ext = c.child_by_field_name("external_name")
            if ext is not None:
                ext_t = txt(ext)
                label = "" if ext_t == "_" else ext_t  # `_ name` suppresses the argument label
            else:
                label = internal  # no external label -> the label IS the parameter name (Swift rule)
            out.append(
                RawParameter(
                    name=internal,
                    type=type_text(type_node),
                    position=pos,
                    label=label,
                    is_variadic=any(g.type == "..." for g in c.children),
                )
            )
            pos += 1
        return out

    def _func_name(node) -> str:
        return next((txt(n) for n in field_children(node, "name") if n.type == "simple_identifier"), "")

    def _return_type(node) -> str | None:
        node_ = next((n for n in field_children(node, "name") if n.type != "simple_identifier"), None)
        return type_text(node_) or None

    def build_function(node, kind: str, has_body: bool) -> RawFunction:
        keywords, attributes, access, is_static = modifiers_of(node)
        ls, le = lines(node)
        return RawFunction(
            name="init" if kind == "INITIALIZER" else _func_name(node),
            kind=kind,
            accessibility=access,
            modifiers=keywords,
            attributes=attributes,
            return_type=None if kind == "INITIALIZER" else _return_type(node),
            parameters=parameters(node),
            calls=collect_calls(node.child_by_field_name("body")),
            is_static=is_static,
            has_body=has_body,
            source=txt(node),
            doc=leading_comment(node, source),
            line_start=ls,
            line_end=le,
        )

    def _pattern_name(pat) -> str:
        """The bound identifier of a `pattern`. A plain `property_declaration`'s name-pattern is
        just the identifier, but a `protocol_property_declaration`'s wraps the var/let binding
        inside the pattern -- so we pull the `simple_identifier` out rather than take the text."""
        if pat is None:
            return ""
        sid = next((c for c in pat.named_children if c.type == "simple_identifier"), None)
        if sid is not None:
            return txt(sid)
        for c in pat.named_children:
            r = _pattern_name(c)
            if r:
                return r
        return txt(pat)

    def build_property(node, is_requirement: bool = False) -> RawProperty:
        keywords, _attrs, access, is_static = modifiers_of(node)
        pat = node.child_by_field_name("name")
        vbp = next((c for c in node.children if c.type == "value_binding_pattern"), None)
        if vbp is None and pat is not None:  # protocol requirements nest the binding in the pattern
            vbp = next((c for c in pat.named_children if c.type == "value_binding_pattern"), None)
        ta = next((c for c in node.children if c.type == "type_annotation"), None)
        ptype = type_text(ta.named_children[0]) if ta is not None and ta.named_children else ""
        ls, le = lines(node)
        return RawProperty(
            name=_pattern_name(pat),
            type=ptype,
            accessibility=access,
            modifiers=keywords,
            is_static=is_static,
            is_computed=is_requirement or node.child_by_field_name("computed_value") is not None,
            is_constant=(txt(vbp) == "let") if vbp is not None else False,
            line_start=ls,
            line_end=le,
        )

    def build_enum_entry(node) -> list[RawEnumMember]:
        out: list[RawEnumMember] = []
        cur: RawEnumMember | None = None
        for i in range(node.child_count):
            child = node.child(i)
            fname = node.field_name_for_child(i)
            if fname == "name":
                cur = RawEnumMember(name=txt(child))
                out.append(cur)
            elif fname == "data_contents" and cur is not None:
                cur.associated_values = [type_text(c) for c in child.named_children]
            elif fname == "raw_value" and cur is not None:
                cur.raw_value = txt(child)
        return out

    def dispatch_member(member, rt: RawType) -> None:
        t = member.type
        if t == "property_declaration":
            rt.properties.append(build_property(member))
        elif t == "protocol_property_declaration":
            rt.properties.append(build_property(member, is_requirement=True))
        elif t == "function_declaration":
            rt.methods.append(build_function(member, "METHOD", has_body=member.child_by_field_name("body") is not None))
        elif t == "protocol_function_declaration":
            rt.methods.append(build_function(member, "METHOD", has_body=False))
        elif t == "init_declaration":
            rt.methods.append(build_function(member, "INITIALIZER", has_body=True))
        elif t == "enum_entry":
            rt.enum_members.extend(build_enum_entry(member))
        elif t == "class_declaration" and _decl_kind(member) != "extension":
            rt.nested_types.append(build_type(member))
        elif t == "protocol_declaration":
            rt.nested_types.append(build_type(member))

    def _decl_kind(node) -> str:
        dk = node.child_by_field_name("declaration_kind")
        return txt(dk) if dk is not None else "protocol"

    def build_type(node) -> RawType:
        if node.type == "protocol_declaration":
            kind = "Interface"
        else:
            kind = _TYPE_KIND[_decl_kind(node)]
        keywords, attributes, access, _is_static = modifiers_of(node)
        ls, le = lines(node)
        rt = RawType(
            kind=kind,
            name=simple_type_name(node.child_by_field_name("name")) or "",
            accessibility=access,
            modifiers=keywords,
            attributes=attributes,
            type_parameters=type_parameters(node),
            inherits=inherited_names(node),
            source=txt(node),
            doc=leading_comment(node, source),
            line_start=ls,
            line_end=le,
        )
        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.named_children:
                dispatch_member(member, rt)
        return rt

    def build_extension(node) -> RawExtension:
        ls, le = lines(node)
        ext = RawExtension(
            type_name=simple_type_name(node.child_by_field_name("name")) or "",
            conformances=inherited_names(node),
            line_start=ls,
            line_end=le,
        )
        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.named_children:
                t = member.type
                if t == "property_declaration":
                    ext.properties.append(build_property(member))
                elif t == "function_declaration":
                    ext.methods.append(build_function(member, "METHOD", has_body=member.child_by_field_name("body") is not None))
                elif t == "init_declaration":
                    ext.methods.append(build_function(member, "INITIALIZER", has_body=True))
        return ext

    rf = RawFile(path=path_label)
    for child in tree.root_node.named_children:
        t = child.type
        if t == "import_declaration":
            ident = next((c for c in child.named_children if c.type in ("identifier", "simple_identifier")), None)
            if ident is not None:
                rf.imports.append(RawImport(name=txt(ident).split(".")[0]))
        elif t == "class_declaration":
            if _decl_kind(child) == "extension":
                rf.extensions.append(build_extension(child))
            else:
                rf.types.append(build_type(child))
        elif t == "protocol_declaration":
            rf.types.append(build_type(child))
        elif t == "function_declaration":
            rf.functions.append(build_function(child, "FUNCTION", has_body=True))
        # top-level `let`/`var` globals (property_declaration at file scope) are intentionally not
        # modelled -- Swift has no source module tier to hang them off, and they're rare.
    return rf
