"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared state
-- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned, no
tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and takes
raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-php 0.24.1
(module `tree_sitter_php`, grammar function `language_php()` -- see grammar.py; there is NO bare
`language()`). Verified via a grammar probe, not assumed:

- top level (`program`): `php_tag`; `namespace_definition` (field `name`=namespace_name, optional
  field `body`=compound_statement for the `namespace X { ... }` block form); `namespace_use_declaration`
  (`namespace_use_clause` children, each a `name`/`qualified_name` + optional field `alias`); and the
  declarations `class_declaration` / `interface_declaration` / `trait_declaration` /
  `enum_declaration` / `function_definition`.
- type heritage: `base_clause` (extends -- one class, or a list of interfaces for an interface) and
  `class_interface_clause` (implements) hold `name`/`qualified_name` children; enums carry an
  optional backing `primitive_type` before the body.
- modifiers are individual NAMED children of the declaration: `abstract_modifier`, `final_modifier`,
  `static_modifier`, `readonly_modifier`, `visibility_modifier` (text = public/private/protected).
- body (`declaration_list`, or `enum_declaration_list` for enums): `method_declaration`
  (field `name`, field `parameters`=formal_parameters, field `return_type`, field `body`),
  `property_declaration` (field `type` + repeated `property_element`, each field `name`=variable_name),
  `const_declaration` (`const_element` children), `use_declaration` (trait use -> `name`/`qualified_name`),
  and `enum_case` (field `name`, optional field `value`).
- params (`formal_parameters`): `simple_parameter`, `variadic_parameter` (`Type ...$x`), and
  `property_promotion_parameter` (field `visibility` -- also becomes a Property); each has field
  `type` and field `name` (a `variable_name`, `$`-prefixed).
- calls: `function_call_expression` (field `function`), `member_call_expression` (field `object`,
  field `name`), `scoped_call_expression` (field `scope`, field `name`), `object_creation_expression`
  (`new` + a `name`/`qualified_name`). `variable_name` = `$` + `name`; `member_access_expression`
  has field `object` + field `name`; `relative_scope` = self/static/parent.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.php.grammar import new_parser
from auryn.ingestors.php.raw_model import (
    RawCall,
    RawConstant,
    RawEnumCase,
    RawFile,
    RawFunction,
    RawMethod,
    RawParameter,
    RawProperty,
    RawType,
    RawUse,
)

_TYPE_DECL_KINDS = {
    "class_declaration": "Class",
    "interface_declaration": "Interface",
    "trait_declaration": "Trait",
    "enum_declaration": "Enum",
}
_NAME_NODES = ("name", "qualified_name")


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

    def type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def simple_name(node) -> str | None:
        """The bare simple name a name/type node denotes: `Base` for `Base`, `App\\Base` for a
        `qualified_name` (last segment), the inner name for a `named_type`/`optional_type`,
        self/static/parent for a `relative_scope`. None if it names nothing."""
        if node is None:
            return None
        if node.type == "name":
            return txt(node)
        if node.type == "qualified_name":
            last = None
            for c in node.children:
                if c.type == "name":
                    last = c
            return txt(last) if last is not None else None
        if node.type == "relative_scope":  # self / static / parent
            return txt(node).strip()
        if node.type in ("named_type", "optional_type"):
            for c in node.children:
                if c.type in _NAME_NODES:
                    return simple_name(c)
        # descend to the first name we can find
        stack = list(node.children)
        while stack:
            n = stack.pop(0)
            if n.type in _NAME_NODES:
                return simple_name(n)
            stack.extend(n.children)
        return None

    def var_name(variable_name_node) -> str:
        """A `variable_name` is `$` + `name`; return the identifier without the `$`."""
        if variable_name_node is None:
            return ""
        return txt(variable_name_node).lstrip("$")

    def visibility_of(node, default: str = "public") -> str:
        for c in node.children:
            if c.type == "visibility_modifier":
                return txt(c).strip()
        return default

    def has_mod(node, modifier_type: str) -> bool:
        return any(c.type == modifier_type for c in node.children)

    # -- names lists (extends / implements / trait use) ------------------------------------

    def name_list(node) -> list[str]:
        if node is None:
            return []
        out = []
        for c in node.children:
            if c.type in _NAME_NODES:
                nm = simple_name(c)
                if nm:
                    out.append(nm)
        return out

    # -- parameters ------------------------------------------------------------------------

    def parameters(formal_parameters_node) -> list[RawParameter]:
        if formal_parameters_node is None:
            return []
        out: list[RawParameter] = []
        pos = 0
        for c in formal_parameters_node.children:
            if c.type == "simple_parameter":
                out.append(RawParameter(name=var_name(field(c, "name")), type=type_text(field(c, "type")), position=pos))
                pos += 1
            elif c.type == "variadic_parameter":
                out.append(RawParameter(name=var_name(field(c, "name")), type=type_text(field(c, "type")), position=pos, is_variadic=True))
                pos += 1
            elif c.type == "property_promotion_parameter":
                out.append(RawParameter(
                    name=var_name(field(c, "name")), type=type_text(field(c, "type")), position=pos,
                    is_promoted=True, visibility=(txt(field(c, "visibility")).strip() or "public"),
                ))
                pos += 1
        return out

    # -- calls -----------------------------------------------------------------------------

    def call_receiver(obj) -> str | None:
        """A simple name for the call's receiver, or None if too complex to name."""
        if obj is None:
            return None
        if obj.type == "variable_name":
            return var_name(obj)  # "this", "store", ...
        if obj.type == "member_access_expression":  # $this->store -> the accessed member "store"
            nm = field(obj, "name")
            return txt(nm) if nm is not None and nm.type == "name" else None
        return None

    def collect_calls(body) -> list[RawCall]:
        if body is None:
            return []
        out: list[RawCall] = []
        seen: set = set()

        def add(name, receiver, kind):
            key = (name, receiver, kind)
            if name and key not in seen:
                seen.add(key)
                out.append(RawCall(name=name, receiver=receiver, kind=kind))

        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "function_call_expression":
                add(simple_name(field(n, "function")), None, "function")
            elif n.type == "member_call_expression":
                nm = field(n, "name")
                if nm is not None and nm.type == "name":
                    add(txt(nm), call_receiver(field(n, "object")), "method")
            elif n.type == "scoped_call_expression":
                nm = field(n, "name")
                if nm is not None and nm.type == "name":
                    add(txt(nm), simple_name(field(n, "scope")), "static")
            elif n.type == "object_creation_expression":
                nm = next((c for c in n.children if c.type in _NAME_NODES), None)
                if nm is not None:
                    add(simple_name(nm), None, "new")
            stack.extend(n.children)
        return out

    # -- members ---------------------------------------------------------------------------

    def build_method(node) -> RawMethod:
        name = txt(field(node, "name"))
        kind = "CONSTRUCTOR" if name.lower() == "__construct" else "ORDINARY"
        body = field(node, "body")
        ls, le = lines(node)
        return RawMethod(
            name=name,
            kind=kind,
            visibility=visibility_of(node),
            is_static=has_mod(node, "static_modifier"),
            is_abstract=has_mod(node, "abstract_modifier"),
            is_final=has_mod(node, "final_modifier"),
            return_type=type_text(field(node, "return_type")) or None if kind == "ORDINARY" else None,
            parameters=parameters(field(node, "parameters")),
            calls=collect_calls(body),
            source=txt(node),
            doc=leading_comment(node, source),
            line_start=ls, line_end=le,
        )

    def build_properties(node) -> list[RawProperty]:
        vis = visibility_of(node)
        is_static = has_mod(node, "static_modifier")
        is_readonly = has_mod(node, "readonly_modifier")
        type_txt = type_text(field(node, "type"))
        ls, le = lines(node)
        out = []
        for c in node.children:
            if c.type == "property_element":
                out.append(RawProperty(
                    name=var_name(field(c, "name")), type=type_txt, visibility=vis,
                    is_static=is_static, is_readonly=is_readonly, line_start=ls, line_end=le,
                ))
        return out

    def build_constants(node) -> list[RawConstant]:
        vis = visibility_of(node)
        ls, le = lines(node)
        out = []
        for c in node.children:
            if c.type == "const_element":
                name_node = next((g for g in c.children if g.type == "name"), None)
                value_node = next((g for g in c.named_children if g.type != "name"), None)
                out.append(RawConstant(
                    name=txt(name_node), visibility=vis, value=type_text(value_node),
                    line_start=ls, line_end=le,
                ))
        return out

    def enum_cases(enum_body) -> list[RawEnumCase]:
        out = []
        for c in enum_body.children:
            if c.type == "enum_case":
                out.append(RawEnumCase(name=txt(field(c, "name")), value=type_text(field(c, "value"))))
        return out

    def dispatch_member(member, rt: RawType) -> None:
        if member.type == "method_declaration":
            rt.methods.append(build_method(member))
        elif member.type == "property_declaration":
            rt.properties.extend(build_properties(member))
        elif member.type == "const_declaration":
            rt.constants.extend(build_constants(member))
        elif member.type == "use_declaration":  # `use TraitName;`
            rt.traits.extend(name_list(member))

    def build_type(node, namespace: str) -> RawType:
        kind = _TYPE_DECL_KINDS[node.type]
        ls, le = lines(node)
        rt = RawType(
            kind=kind, name=txt(field(node, "name")), namespace=namespace,
            is_abstract=has_mod(node, "abstract_modifier"), is_final=has_mod(node, "final_modifier"),
            source=txt(node), doc=leading_comment(node, source), line_start=ls, line_end=le,
        )

        base = next((c for c in node.children if c.type == "base_clause"), None)
        impl = next((c for c in node.children if c.type == "class_interface_clause"), None)
        if kind == "Class":
            bases = name_list(base)
            rt.superclass = bases[0] if bases else None
            rt.interfaces = name_list(impl)
        elif kind == "Interface":
            rt.interfaces = name_list(base)  # an interface's base_clause lists super-interfaces
        elif kind == "Enum":
            rt.interfaces = name_list(impl)
            rt.backing_type = next((txt(c) for c in node.children if c.type == "primitive_type"), None)

        body = field(node, "body")
        if body is not None:
            for member in body.children:
                dispatch_member(member, rt)
            if body.type == "enum_declaration_list":
                rt.enum_cases = enum_cases(body)

        # constructor property promotion -> Property members (also kept as params on the ctor).
        for m in rt.methods:
            if m.kind == "CONSTRUCTOR":
                for p in m.parameters:
                    if p.is_promoted:
                        rt.properties.append(RawProperty(
                            name=p.name, type=p.type, visibility=p.visibility or "public",
                            is_promoted=True, line_start=m.line_start, line_end=m.line_start,
                        ))
        return rt

    def build_function(node) -> RawFunction:
        ls, le = lines(node)
        return RawFunction(
            name=txt(field(node, "name")),
            return_type=type_text(field(node, "return_type")) or None,
            parameters=parameters(field(node, "parameters")),
            calls=collect_calls(field(node, "body")),
            source=txt(node), doc=leading_comment(node, source), line_start=ls, line_end=le,
        )

    # -- top-level walk (tracks the current namespace) -------------------------------------

    def walk_top(nodes, current_ns: str) -> None:
        for child in nodes:
            if child.type == "namespace_definition":
                ns = txt(field(child, "name"))
                body = field(child, "body")  # `namespace X { ... }` block form
                if body is not None:
                    walk_top(body.children, ns)
                else:  # `namespace X;` -- applies to the following siblings
                    current_ns = ns
                    if not rf.namespace:
                        rf.namespace = ns
            elif child.type == "namespace_use_declaration":
                for clause in child.children:
                    if clause.type == "namespace_use_clause":
                        target = next((c for c in clause.children if c.type in _NAME_NODES), None)
                        alias = field(clause, "alias")
                        if target is not None:
                            rf.uses.append(RawUse(name=txt(target), alias=txt(alias) if alias is not None else None))
            elif child.type in _TYPE_DECL_KINDS:
                rf.types.append(build_type(child, current_ns))
            elif child.type == "function_definition":
                rf.functions.append(build_function(child))

    walk_top(tree.root_node.children, "")
    return rf
