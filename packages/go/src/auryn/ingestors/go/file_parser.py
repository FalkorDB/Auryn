"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned,
no tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-go 0.25.0
(see the package's grammar probe), not assumed:

- top level (`source_file` children): `package_clause` (child `package_identifier`),
  `import_declaration` (-> `import_spec_list` -> `import_spec`, or a single `import_spec`),
  `const_declaration` (-> `const_spec`), `var_declaration` (ignored), `type_declaration`, and
  `function_declaration` / `method_declaration`.
- `import_spec`: field `path` (interpreted_string_literal, quotes stripped) + optional field
  `name` (package_identifier alias).
- `type_declaration` children: `type_spec` (field `name` type_identifier, optional field
  `type_parameters` = type_parameter_list, field `type` = struct_type|interface_type|other) and
  `type_alias` (`type X = Y`: type_identifier + `=` + aliased type).
- `struct_type` -> `field_declaration_list` -> `field_declaration`: a NAMED field has one or more
  `field_identifier` children + a `type`; an EMBEDDED/anonymous field has NO field_identifier
  (just a type, possibly under `pointer_type` / a `qualified_type`).
- `interface_type` children: `method_elem` (field `field` name = field_identifier, `parameters`
  = parameter_list, `result`) and `type_elem` (an embedded interface / type constraint).
- `function_declaration`: fields `name` (identifier), `parameters` (parameter_list), `result`
  (a type node or a parameter_list for multiple returns), `body` (block).
- `method_declaration`: as above plus field `receiver` (parameter_list holding one
  parameter_declaration: name identifier + type, the type possibly `pointer_type` ->
  `generic_type` -> type_identifier).
- `parameter_declaration`: one or more `identifier` names sharing a `type` (`a, b int`);
  `variadic_parameter_declaration` for `nums ...int`.
- calls: `call_expression` field `function` = `identifier` (bare `foo()`; `new`/builtins handled
  specially) or `selector_expression` (fields `operand`, `field`) for `x.foo()`; construction is
  `composite_literal` (field `type`) and `new(T)`.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.go import http_idioms
from auryn.ingestors.go.grammar import new_parser
from auryn.ingestors.go.raw_model import (
    RawCall,
    RawConst,
    RawField,
    RawFile,
    RawFunction,
    RawImport,
    RawParameter,
    RawReceiver,
    RawType,
    RawTypeMethod,
)

# Go predeclared type names / literals that never name a project type (dropped from type_refs).
_BUILTIN_TYPES = frozenset(
    {
        "bool", "byte", "rune", "string", "error", "uintptr", "any", "comparable",
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64",
        "float32", "float64", "complex64", "complex128",
        "true", "false", "nil", "iota",
    }
)
# Builtin functions -- a bare call to one of these is never a project function (no CALLS edge).
# `new` is handled before this check (it is a construction site, not a call).
_BUILTIN_FUNCS = frozenset(
    {
        "append", "cap", "clear", "close", "complex", "copy", "delete", "imag", "len",
        "make", "max", "min", "new", "panic", "print", "println", "real", "recover",
    }
)


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)
    rf = RawFile(path=path_label)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def fieldn(node, name):
        return node.child_by_field_name(name) if node is not None else None

    def type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def type_refs(node) -> list[str]:
        """Project-type reference names a type expression mentions: every `type_identifier` (as
        "Name") and every `qualified_type` (as "pkg.Name"), skipping predeclared builtins. Reads
        the AST -- not the text -- so a `parameter_list` result with *named* returns
        (`(n int, err error)`) contributes only the type identifiers, never the value names."""
        if node is None:
            return []
        out: list[str] = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "qualified_type":
                pkg = next((c for c in n.children if c.type == "package_identifier"), None)
                name = next((c for c in n.children if c.type == "type_identifier"), None)
                if pkg is not None and name is not None:
                    out.append(f"{txt(pkg)}.{txt(name)}")
                continue  # don't descend: its inner type_identifier is the qualified name
            if n.type == "type_identifier":
                name = txt(n)
                if name not in _BUILTIN_TYPES:
                    out.append(name)
                continue
            stack.extend(n.children)
        return out

    def simple_type_name(node):
        """The bare simple name a receiver/embed type denotes, stripping `*` and generic args:
        `Store` for `Store`, `*Store`, `Store[T]`, `*Store[T]`. None for a qualified type."""
        if node is None:
            return None
        if node.type == "type_identifier":
            return txt(node)
        if node.type in ("pointer_type", "generic_type", "parenthesized_type"):
            for c in node.named_children:
                nm = simple_type_name(c)
                if nm is not None:
                    return nm
        return None

    # -- parameters -------------------------------------------------------------------------

    def _param_type_node(pd):
        node = fieldn(pd, "type")
        if node is not None:
            return node
        return next((c for c in pd.named_children if c.type != "identifier"), None)

    def parameters(param_list) -> list[RawParameter]:
        if param_list is None:
            return []
        out: list[RawParameter] = []
        pos = 0
        for c in param_list.children:
            if c.type == "parameter_declaration":
                t = type_text(_param_type_node(c))
                names = [g for g in c.children if g.type == "identifier"]
                if names:
                    for nm in names:
                        out.append(RawParameter(name=txt(nm), type=t, position=pos))
                        pos += 1
                else:  # unnamed parameter (interface spec, `func(int)`, results)
                    out.append(RawParameter(name="", type=t, position=pos))
                    pos += 1
            elif c.type == "variadic_parameter_declaration":
                t = type_text(_param_type_node(c))
                nm = next((g for g in c.children if g.type == "identifier"), None)
                out.append(
                    RawParameter(name=txt(nm) if nm else "", type=t, position=pos, is_variadic=True)
                )
                pos += 1
        return out

    def signature_refs(param_list, result) -> list[str]:
        refs: list[str] = []
        if param_list is not None:
            for c in param_list.children:
                if c.type in ("parameter_declaration", "variadic_parameter_declaration"):
                    refs.extend(type_refs(_param_type_node(c)))
        refs.extend(type_refs(result))
        return refs

    # -- call / construction extraction -----------------------------------------------------

    def collect_calls(body):
        """(calls, construct_refs) from a function/method body. A selector call keeps its
        operand identifier as the receiver; construction (`T{}`, `&T{}`, `new(T)`) yields
        type refs, not calls (Go has no constructor to CALL)."""
        calls: list[RawCall] = []
        construct_refs: list[str] = []
        if body is None:
            return calls, construct_refs
        seen: set = set()
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                fn = fieldn(n, "function")
                if fn is not None and fn.type == "identifier":
                    name = txt(fn)
                    if name == "new":
                        args = fieldn(n, "arguments")
                        if args is not None:
                            first = next((a for a in args.named_children), None)
                            construct_refs.extend(type_refs(first))
                    elif name and name not in _BUILTIN_FUNCS:
                        key = (name, None)
                        if key not in seen:
                            seen.add(key)
                            calls.append(RawCall(name=name, receiver=None))
                elif fn is not None and fn.type == "selector_expression":
                    operand = fieldn(fn, "operand")
                    field_node = fieldn(fn, "field")
                    name = txt(field_node)
                    recv = txt(operand) if operand is not None and operand.type == "identifier" else None
                    if name and recv is not None:
                        key = (name, recv)
                        if key not in seen:
                            seen.add(key)
                            calls.append(RawCall(name=name, receiver=recv))
            elif n.type == "composite_literal":
                construct_refs.extend(type_refs(fieldn(n, "type")))
            stack.extend(n.children)
        return calls, construct_refs

    # -- functions / methods ----------------------------------------------------------------

    def receiver_of(node):
        recv = fieldn(node, "receiver")
        if recv is None:
            return None
        pd = next((c for c in recv.children if c.type == "parameter_declaration"), None)
        if pd is None:
            return None
        type_node = _param_type_node(pd)
        name = next((c for c in pd.children if c.type == "identifier"), None)
        return RawReceiver(
            name=txt(name) if name is not None else "",
            type_name=simple_type_name(type_node) or "",
            is_pointer=type_node is not None and type_node.type == "pointer_type",
        )

    http_ctx: list = []  # the file's HttpImports, built once on first use (Go puts imports first)

    def build_function(node, receiver) -> RawFunction:
        params = parameters(fieldn(node, "parameters"))
        result = fieldn(node, "result")
        body = fieldn(node, "body")
        calls, construct_refs = collect_calls(body)
        ls, le = lines(node)
        refs = signature_refs(fieldn(node, "parameters"), result)
        refs.extend(construct_refs)
        if not http_ctx:
            http_ctx.append(http_idioms.import_context(rf.imports))
        return RawFunction(
            name=txt(fieldn(node, "name")),
            receiver=receiver,
            parameters=params,
            return_type=type_text(result),
            type_refs=refs,
            calls=calls,
            http_calls=http_idioms.client_calls(body, source, http_ctx[0], params),
            http_routes=http_idioms.routes(body, source, http_ctx[0], params, receiver),
            source=txt(node),
            doc=leading_comment(node, source),
            line_start=ls,
            line_end=le,
        )

    # -- struct / interface / alias ---------------------------------------------------------

    def type_parameters(node) -> list[str]:
        tp = fieldn(node, "type_parameters")
        if tp is None:
            return []
        out = []
        for c in tp.named_children:
            if c.type == "type_parameter_declaration":
                nm = fieldn(c, "name") or next((g for g in c.children if g.type == "identifier"), None)
                if nm is not None:
                    out.append(txt(nm))
        return out

    def build_struct_fields(struct_node):
        fdl = next((c for c in struct_node.children if c.type == "field_declaration_list"), None)
        fields: list[RawField] = []
        embeds: list[str] = []
        if fdl is None:
            return fields, embeds
        for fd in fdl.children:
            if fd.type != "field_declaration":
                continue
            names = [c for c in fd.children if c.type == "field_identifier"]
            type_node = fieldn(fd, "type")
            if not names:  # anonymous/embedded field -> EMBEDS
                embed_node = type_node
                if embed_node is None:  # `Base` / `*Base` / `pkg.Base` with no `type` field
                    embed_node = next(
                        (c for c in fd.named_children if c.type != "field_identifier"), None
                    )
                refs = type_refs(embed_node)
                if refs:
                    embeds.append(refs[0])
                continue
            t = type_text(type_node)
            refs = type_refs(type_node)
            ls, le = lines(fd)
            for nm in names:
                fields.append(
                    RawField(name=txt(nm), type=t, type_refs=list(refs), line_start=ls, line_end=le)
                )
        return fields, embeds

    def build_interface(iface_node):
        methods: list[RawTypeMethod] = []
        embeds: list[str] = []
        for c in iface_node.children:
            if c.type in ("method_elem", "method_spec"):
                name = fieldn(c, "name") or next(
                    (g for g in c.children if g.type == "field_identifier"), None
                )
                ls, le = lines(c)
                methods.append(
                    RawTypeMethod(
                        name=txt(name),
                        parameters=parameters(fieldn(c, "parameters")),
                        return_type=type_text(fieldn(c, "result")),
                        doc=leading_comment(c, source),
                        line_start=ls,
                        line_end=le,
                    )
                )
            elif c.type == "type_elem":
                refs = type_refs(c)
                if refs:
                    embeds.append(refs[0])
        return methods, embeds

    def build_type(spec_node, is_alias: bool, decl_node):
        name_node = fieldn(spec_node, "name") or next(
            (c for c in spec_node.children if c.type == "type_identifier"), None
        )
        ls, le = lines(spec_node)
        tparams = type_parameters(spec_node)
        # A per-spec comment inside a grouped `type ( ... )` block attaches to the spec; a
        # single `type Foo ...`'s doc sits above the enclosing type_declaration instead.
        doc = leading_comment(spec_node, source) or leading_comment(decl_node, source)
        # Both `type_spec` and `type_alias` expose the underlying type via the `type` field.
        underlying_node = fieldn(spec_node, "type")
        if underlying_node is None:  # defensive: fall back to the last non-name child
            underlying_node = next((c for c in spec_node.named_children if c is not name_node), None)

        if underlying_node is not None and underlying_node.type == "struct_type":
            fields, embeds = build_struct_fields(underlying_node)
            return RawType(
                kind="Struct", name=txt(name_node), type_parameters=tparams, fields=fields,
                embeds=embeds, source=txt(spec_node), doc=doc, line_start=ls, line_end=le,
            )
        if underlying_node is not None and underlying_node.type == "interface_type":
            methods, embeds = build_interface(underlying_node)
            return RawType(
                kind="Interface", name=txt(name_node), type_parameters=tparams, methods=methods,
                embeds=embeds, source=txt(spec_node), doc=doc, line_start=ls, line_end=le,
            )
        return RawType(
            kind="TypeAlias", name=txt(name_node), type_parameters=tparams,
            underlying=type_text(underlying_node), is_alias=is_alias,
            source=txt(spec_node), doc=doc, line_start=ls, line_end=le,
        )

    # -- imports / consts -------------------------------------------------------------------

    def build_import(spec_node) -> RawImport:
        path_node = fieldn(spec_node, "path")
        path = txt(path_node).strip('"`')
        alias_node = fieldn(spec_node, "name")
        alias = txt(alias_node) if alias_node is not None and alias_node.type == "package_identifier" else None
        return RawImport(path=path, alias=alias)

    def build_consts(node) -> list[RawConst]:
        out: list[RawConst] = []
        for spec in node.children:
            if spec.type != "const_spec":
                continue
            names = [c for c in spec.children if c.type == "identifier"]
            values = fieldn(spec, "value")
            value_text = type_text(values) if values is not None else ""
            if value_text == "":
                exprs = next((c for c in spec.children if c.type == "expression_list"), None)
                value_text = " ".join(txt(exprs).split()) if exprs is not None else ""
            ls, le = lines(spec)
            for nm in names:
                out.append(RawConst(name=txt(nm), value=value_text, line_start=ls, line_end=le))
        return out

    # -- top level --------------------------------------------------------------------------

    for child in tree.root_node.children:
        t = child.type
        if t == "package_clause":
            name_node = next((c for c in child.children if c.type == "package_identifier"), None)
            rf.package = txt(name_node)
        elif t == "import_declaration":
            for c in child.children:
                if c.type == "import_spec":
                    rf.imports.append(build_import(c))
                elif c.type == "import_spec_list":
                    for spec in c.children:
                        if spec.type == "import_spec":
                            rf.imports.append(build_import(spec))
        elif t == "const_declaration":
            rf.consts.extend(build_consts(child))
        elif t == "type_declaration":
            for c in child.children:
                if c.type == "type_spec":
                    rf.types.append(build_type(c, is_alias=False, decl_node=child))
                elif c.type == "type_alias":
                    rf.types.append(build_type(c, is_alias=True, decl_node=child))
        elif t == "function_declaration":
            rf.functions.append(build_function(child, receiver=None))
        elif t == "method_declaration":
            rf.functions.append(build_function(child, receiver=receiver_of(child)))

    return rf
