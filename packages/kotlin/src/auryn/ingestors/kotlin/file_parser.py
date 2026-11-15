"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared state
-- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned, no
tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-kotlin 1.1.0
(see the package's grammar probe), not assumed. Its grammar is ANTLR-derived, so it differs from
tree-sitter-java's in several ways handled here:

- top level (`source_file`): `package_header` (child `qualified_identifier`), `import` (child
  `qualified_identifier` + optional `.`/`*` for a wildcard + optional `as` alias), plus the
  declarations `class_declaration` / `object_declaration` (top-level singleton) /
  `function_declaration` (top-level fun) / `property_declaration` (top-level val/var).
- `class_declaration` covers class/interface/enum: distinguished by the anonymous `class` vs
  `interface` keyword child, and (for enum) an `enum` class_modifier or an `enum_class_body`.
  `object_declaration` and `companion_object` are singletons modelled as Class.
- heritage lives in `delegation_specifiers`: a `delegation_specifier` wrapping a
  `constructor_invocation` (has `()`, e.g. `Base()`) is the SUPERCLASS; a bare `user_type`
  (`Named`) is an interface -- so EXTENDS vs IMPLEMENTS needs no naming heuristic.
- a `primary_constructor` holds `class_parameter`s; a `val`/`var` class_parameter is BOTH a
  constructor Parameter and a promoted property Field.
- modifiers are wrapper nodes (`visibility_modifier`/`inheritance_modifier`/`class_modifier`/...)
  under a `modifiers` node; `annotation` is its own child. A type node may be `user_type` or
  `nullable_type` (`T?`); a user_type's simple name is its LAST direct `identifier` child (so
  `com.acme.Base<T>` -> `Base`).
- calls: `call_expression` whose callee is a bare `identifier` (`foo()` / `Foo()`) or a
  `navigation_expression` (`store.put()`, `this.bar()`); Kotlin has no `new`, so a receiver-less
  call naming a project type is resolved as a constructor later, in resolution.py.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.kotlin.grammar import new_parser
from auryn.ingestors.kotlin.raw_model import (
    RawAnnotation,
    RawCall,
    RawEnumMember,
    RawField,
    RawFile,
    RawFunction,
    RawImport,
    RawParameter,
    RawType,
)

_ACCESS_KEYWORDS = ("public", "private", "protected", "internal")
_TYPE_NODES = frozenset({"user_type", "nullable_type", "function_type", "parenthesized_type"})
# Type-body nodes; a `class_declaration` with an `enum_class_body` (or an `enum` modifier) is an Enum.
_BODY_NODES = frozenset({"class_body", "enum_class_body"})
_NESTED_DECL_NODES = frozenset({"class_declaration", "object_declaration", "companion_object"})


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

    def simple_type_name(node) -> str | None:
        """The bare simple name a type node denotes: `Base` for `Base`, `Base<T>`, `com.acme.Base`
        (last segment), or `String?` (unwraps nullable_type). None if it names nothing."""
        if node is None:
            return None
        t = node.type
        if t == "identifier":
            return txt(node)
        if t == "nullable_type":
            inner = next((c for c in node.children if c.type in _TYPE_NODES), None)
            return simple_type_name(inner)
        if t == "constructor_invocation":
            return simple_type_name(next((c for c in node.children if c.type == "user_type"), None))
        if t == "user_type":
            # last direct identifier before any type_arguments is the simple name.
            ident = None
            for c in node.children:
                if c.type == "identifier":
                    ident = c
                elif c.type == "type_arguments":
                    break
            return txt(ident) if ident is not None else None
        for c in node.children:  # descend (parenthesized_type, ...)
            r = simple_type_name(c)
            if r:
                return r
        return None

    def type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def type_node_of(node):
        return next((c for c in node.children if c.type in _TYPE_NODES), None)

    def annotation_of(node) -> RawAnnotation:
        target = next((c for c in node.children if c.type in ("user_type", "constructor_invocation")), None)
        name = simple_type_name(target) if target is not None else txt(node).lstrip("@").split("(")[0].strip()
        return RawAnnotation(name=name or "")

    def modifiers_of(node) -> tuple[list[str], list[RawAnnotation]]:
        """(keyword modifiers, annotations) from a declaration's `modifiers` child. Each modifier
        is a wrapper node (visibility_modifier/inheritance_modifier/class_modifier/...) whose text
        is the keyword; `annotation` children are pulled out separately."""
        mods = next((c for c in node.children if c.type == "modifiers"), None)
        if mods is None:
            return [], []
        keywords: list[str] = []
        annotations: list[RawAnnotation] = []
        for c in mods.children:
            if c.type == "annotation":
                annotations.append(annotation_of(c))
            else:
                kw = txt(c).strip()
                if kw:
                    keywords.append(kw)
        return keywords, annotations

    def accessibility(keywords: list[str]) -> str:
        for kw in _ACCESS_KEYWORDS:
            if kw in keywords:
                return kw
        return "public"  # Kotlin's default

    def type_parameters(node) -> list[str]:
        tp = next((c for c in node.children if c.type == "type_parameters"), None)
        if tp is None:
            return []
        out = []
        for c in tp.children:
            if c.type == "type_parameter":
                ident = next((g for g in c.children if g.type == "identifier"), None)
                if ident is not None:
                    out.append(txt(ident))
        return out

    def delegation(node) -> tuple[str | None, list[str]]:
        ds = next((c for c in node.children if c.type == "delegation_specifiers"), None)
        if ds is None:
            return None, []
        superclass: str | None = None
        interfaces: list[str] = []
        for spec in ds.children:
            if spec.type != "delegation_specifier":
                continue
            child = spec.child(0)
            if child is None:
                continue
            if child.type == "constructor_invocation":  # `Base()` -> the single superclass
                nm = simple_type_name(child)
                if nm and superclass is None:
                    superclass = nm
            elif child.type in ("user_type", "nullable_type"):  # `Named` -> an interface
                nm = simple_type_name(child)
                if nm:
                    interfaces.append(nm)
            elif child.type == "explicit_delegation":  # `I by impl` -> I is an interface
                nm = simple_type_name(type_node_of(child))
                if nm:
                    interfaces.append(nm)
        return superclass, interfaces

    def parameters(fvp) -> list[RawParameter]:
        """function_value_parameters -> RawParameters. `vararg` arrives as a preceding
        `parameter_modifiers` sibling; a default `= value` as a following `=` sibling."""
        if fvp is None:
            return []
        out: list[RawParameter] = []
        pos = 0
        pending_vararg = False
        kids = fvp.children
        for i, c in enumerate(kids):
            if c.type == "parameter_modifiers":
                pending_vararg = "vararg" in txt(c)
            elif c.type == "parameter":
                name_node = next((g for g in c.children if g.type == "identifier"), None)
                out.append(
                    RawParameter(
                        name=txt(name_node), type=type_text(type_node_of(c)), position=pos,
                        is_vararg=pending_vararg, has_default=_has_default_after(kids, i),
                    )
                )
                pos += 1
                pending_vararg = False
        return out

    def _has_default_after(kids, i) -> bool:
        for j in range(i + 1, len(kids)):
            t = kids[j].type
            if t == "=":
                return True
            if t in ("parameter", "parameter_modifiers", ")"):
                break
        return False

    def class_parameters(cp_node) -> tuple[list[RawParameter], list[RawField]]:
        """primary-constructor class_parameters -> (ctor params, promoted `val`/`var` fields)."""
        params: list[RawParameter] = []
        fields: list[RawField] = []
        pos = 0
        for c in (cp_node.children if cp_node is not None else []):
            if c.type != "class_parameter":
                continue
            kws, anns = modifiers_of(c)
            is_val = any(g.type == "val" for g in c.children)
            is_var = any(g.type == "var" for g in c.children)
            name_node = next((g for g in c.children if g.type == "identifier"), None)
            type_node = type_node_of(c)
            ls, le = lines(c)
            params.append(
                RawParameter(
                    name=txt(name_node), type=type_text(type_node), position=pos,
                    is_vararg="vararg" in kws, has_default=any(g.type == "=" for g in c.children),
                )
            )
            pos += 1
            if is_val or is_var:  # a `val`/`var` ctor param is promoted to a property Field
                fields.append(
                    RawField(
                        name=txt(name_node), type=type_text(type_node), accessibility=accessibility(kws),
                        modifiers=kws, is_mutable=is_var, annotations=anns, line_start=ls, line_end=le,
                    )
                )
        return params, fields

    def collect_calls(node) -> list[RawCall]:
        if node is None:
            return []
        out: list[RawCall] = []
        seen: set = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                callee = n.child(0)
                name = recv = None
                if callee is not None:
                    if callee.type == "identifier":
                        name = txt(callee)
                    elif callee.type == "navigation_expression":
                        name = _nav_member(callee)
                        recv = _nav_receiver(callee)
                if name:
                    key = (name, recv)
                    if key not in seen:
                        seen.add(key)
                        out.append(RawCall(name=name, receiver=recv))
            stack.extend(n.children)
        return out

    def _nav_member(nav) -> str | None:
        ident = None
        for c in nav.children:
            if c.type == "identifier":
                ident = c  # the last identifier is the member being accessed
        return txt(ident) if ident is not None else None

    def _nav_receiver(nav) -> str | None:
        first = nav.child(0)
        if first is None:
            return None
        if first.type == "this_expression":
            return "this"
        if first.type == "identifier":
            return txt(first)
        return None  # a chain / call / index receiver -- too complex to name

    def return_type_of(fn_node) -> str | None:
        seen_params = False
        for c in fn_node.children:
            if c.type == "function_value_parameters":
                seen_params = True
                continue
            if not seen_params:
                continue
            if c.type == "function_body":
                break
            if c.type in _TYPE_NODES:
                return type_text(c)
        return None

    def build_function(node) -> RawFunction:
        kws, anns = modifiers_of(node)
        fvp = next((c for c in node.children if c.type == "function_value_parameters"), None)
        body = next((c for c in node.children if c.type == "function_body"), None)
        ls, le = lines(node)
        return RawFunction(
            name=txt(field(node, "name")), kind="ORDINARY", accessibility=accessibility(kws),
            modifiers=kws, return_type=return_type_of(node), parameters=parameters(fvp),
            annotations=anns, calls=collect_calls(body), source=txt(node),
            doc=leading_comment(node, source), line_start=ls, line_end=le,
        )

    def build_secondary_ctor(node, class_name: str) -> RawFunction:
        kws, anns = modifiers_of(node)
        fvp = next((c for c in node.children if c.type == "function_value_parameters"), None)
        body = next((c for c in node.children if c.type == "block"), None)
        ls, le = lines(node)
        return RawFunction(
            name=class_name, kind="CONSTRUCTOR", accessibility=accessibility(kws), modifiers=kws,
            parameters=parameters(fvp), annotations=anns, calls=collect_calls(body),
            source=txt(node), doc=leading_comment(node, source), line_start=ls, line_end=le,
        )

    def build_properties(node) -> list[RawField]:
        kws, anns = modifiers_of(node)
        is_var = any(c.type == "var" for c in node.children)
        ls, le = lines(node)
        out: list[RawField] = []
        for c in node.children:
            decls = [c] if c.type == "variable_declaration" else (
                [g for g in c.children if g.type == "variable_declaration"] if c.type == "multi_variable_declaration" else []
            )
            for vd in decls:
                name_node = next((g for g in vd.children if g.type == "identifier"), None)
                out.append(
                    RawField(
                        name=txt(name_node), type=type_text(type_node_of(vd)),
                        accessibility=accessibility(kws), modifiers=kws, is_mutable=is_var,
                        annotations=anns, line_start=ls, line_end=le,
                    )
                )
        return out

    def build_enum_entry(node) -> RawEnumMember:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        va = next((c for c in node.children if c.type == "value_arguments"), None)
        args = [txt(a) for a in va.named_children] if va is not None else []
        return RawEnumMember(name=txt(name_node), arguments=args)

    def dispatch_member(member, rt: RawType) -> None:
        if member.type == "property_declaration":
            rt.fields.extend(build_properties(member))
        elif member.type == "function_declaration":
            rt.methods.append(build_function(member))
        elif member.type == "secondary_constructor":
            rt.methods.append(build_secondary_ctor(member, rt.name))
        elif member.type == "enum_entry":
            rt.enum_members.append(build_enum_entry(member))
        elif member.type in _NESTED_DECL_NODES:
            rt.nested_types.append(build_type(member, rt.package))

    def build_type(node, package: str) -> RawType:
        kws, anns = modifiers_of(node)
        ntype = node.type
        is_object = ntype in ("object_declaration", "companion_object")
        is_companion = ntype == "companion_object"
        body = next((c for c in node.children if c.type in _BODY_NODES), None)
        if ntype == "class_declaration":
            keyword = next((c for c in node.children if not c.is_named and c.type in ("class", "interface")), None)
            if keyword is not None and keyword.type == "interface":
                kind = "Interface"
            elif (body is not None and body.type == "enum_class_body") or "enum" in kws:
                kind = "Enum"
            else:
                kind = "Class"
        else:  # object_declaration / companion_object -> a singleton, modelled as Class
            kind = "Class"

        name = txt(field(node, "name"))
        if is_companion and not name:
            name = "Companion"  # an unnamed companion object's implicit name

        superclass, interfaces = delegation(node)
        ls, le = lines(node)
        rt = RawType(
            kind=kind, name=name, package=package, accessibility=accessibility(kws), modifiers=kws,
            type_parameters=type_parameters(node), superclass=superclass, interfaces=interfaces,
            is_object=is_object, is_companion=is_companion, is_data="data" in kws, annotations=anns,
            source=txt(node), doc=leading_comment(node, source), line_start=ls, line_end=le,
        )

        pc = next((c for c in node.children if c.type == "primary_constructor"), None)
        if pc is not None:
            cparams = next((c for c in pc.children if c.type == "class_parameters"), None)
            params, prop_fields = class_parameters(cparams)
            pls, ple = lines(pc)
            rt.methods.append(
                RawFunction(
                    name=name, kind="CONSTRUCTOR", accessibility=accessibility(kws),
                    parameters=params, source=txt(pc), line_start=pls, line_end=ple,
                )
            )
            rt.fields.extend(prop_fields)

        if body is not None:
            for member in body.children:
                dispatch_member(member, rt)
        return rt

    def build_import(node) -> RawImport:
        qi = next((c for c in node.children if c.type == "qualified_identifier"), None)
        is_wildcard = any(c.type == "*" for c in node.children)
        alias = None
        kids = node.children
        for i, c in enumerate(kids):
            if c.type == "as":
                alias = next((txt(d) for d in kids[i + 1:] if d.type == "identifier"), None)
                break
        return RawImport(name=txt(qi), is_wildcard=is_wildcard, alias=alias)

    for child in tree.root_node.children:
        if child.type == "package_header":
            qi = next((c for c in child.children if c.type == "qualified_identifier"), None)
            rf.package = txt(qi)
        elif child.type == "import":
            rf.imports.append(build_import(child))
        elif child.type in ("class_declaration", "object_declaration"):
            rf.types.append(build_type(child, rf.package))
        elif child.type == "function_declaration":
            rf.functions.append(build_function(child))
        elif child.type == "property_declaration":
            rf.properties.extend(build_properties(child))

    return rf
