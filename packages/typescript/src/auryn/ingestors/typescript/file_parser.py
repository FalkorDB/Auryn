"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned,
no tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic
and takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-typescript
0.23.2 (see the package's grammar probes), not assumed:

- top level: `import_statement` (import_clause + [source] string), `export_statement`
  (wraps [declaration], or is a re-export carrying [source]), `function_declaration`
  ([name],[parameters],[return_type],[body]), `lexical_declaration`/`variable_declaration`
  (-> variable_declarator [name]/[value] for `const f = () => ...`), `class_declaration` /
  `abstract_class_declaration`, `interface_declaration`, `enum_declaration`,
  `type_alias_declaration`.
- class members: `method_definition` and `public_field_definition`; modifiers `readonly`,
  `static`, `abstract`, `get`/`set`, `?` are ANONYMOUS children (checked by `.type`), while
  `accessibility_modifier` is a named child. Constructor params carrying an
  `accessibility_modifier`/`readonly` are TS "parameter properties" -> also emitted as
  Property.
- heritage: `class_heritage` -> `extends_clause` ([value], generics in a sibling
  type_arguments so [value] is already the bare base) + `implements_clause` (type_identifier
  / generic_type children); interfaces use `extends_type_clause` ([type] children).
- calls: `call_expression` [function] (identifier | member_expression [object]/[property]) and
  `new_expression` [constructor]; `super(...)` is a `super` node and is ignored.
"""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.typescript.grammar import dialect_for, is_declaration, new_parser
from auryn.ingestors.typescript.raw_model import (
    RawCall,
    RawClass,
    RawEnum,
    RawFile,
    RawFunction,
    RawImport,
    RawInterface,
    RawMethod,
    RawParameter,
    RawProperty,
    RawTypeAlias,
)

# Callee wrappers that are transparent to a call's target -- unwrapped so the inner identifier is the
# recorded call site (what SCIP anchors its occurrence to): `(f)()`, `f!()`, `(f as T)()`,
# `(f satisfies T)()`, `(<T>f)()`.
_TRANSPARENT_CALLEES = {"parenthesized_expression", "non_null_expression", "as_expression",
                        "satisfies_expression", "type_assertion"}
# The callee-bearing child inside such a wrapper -- picked regardless of position, so a wrapper that ALSO
# holds a type argument (`f as T`, `<T>f`, where the type may come first) unwraps to the expression, not the
# type. Unknown inner shapes just yield no call site (no regression), never the wrong node.
_CALLEE_EXPR_NODES = {"identifier", "member_expression", "call_expression", "subscript_expression",
                      *_TRANSPARENT_CALLEES}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_FUNC_VALUE_KINDS = {"arrow_function": "ARROW", "function_expression": "FUNCTION_EXPR"}
_LEXICAL = {"lexical_declaration", "variable_declaration"}

# HTTP-client call detection (ADR 0006). Kept STRICT to avoid false positives + a per-call
# perf hit: a bare `x.get(...)` is Map/collection access, NOT HTTP -- a verb-named call counts
# only when its receiver is a known HTTP client. The argument walk that extracts the URL runs
# ONLY when `_is_http_idiom` is true, so non-HTTP calls pay just one set lookup.
_HTTP_CLIENT_RECEIVERS = {"$", "jquery", "axios", "http", "httpclient"}
_HTTP_VERB_METHODS = {"ajax", "get", "post", "put", "delete", "patch", "getjson", "request"}
_HTTP_VERB_TO_METHOD = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
                        "patch": "PATCH", "getjson": "GET"}


def _is_http_idiom(name: str, receiver: str | None, client_vars: frozenset[str] = frozenset()) -> bool:
    n = name.lower()
    if n in ("fetch", "axios") and receiver is None:  # fetch("…"), axios("…")
        return True
    if receiver is None:
        return False
    if receiver.lower() in _HTTP_CLIENT_RECEIVERS and n in _HTTP_VERB_METHODS:
        return True
    # A configured client instance -- `const api = axios.create(...)` -> api.get(...). The var
    # names are discovered per file (precise, so no false positives on an arbitrary `x.get()`).
    return receiver in client_vars and n in _HTTP_VERB_METHODS


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser(path_label).parse(source)
    source_is_ascii = source.isascii()  # when True, a byte column == its UTF-16 column (callee-column fast path)
    u16_cache: dict[int, tuple[list[int], list[int]]] = {}  # line-start byte -> (non-ASCII boundaries, deltas)
    rf = RawFile(
        path=path_label,
        dialect=dialect_for(path_label),
        is_declaration=is_declaration(path_label),
        has_error=tree.root_node.has_error,  # broken/mid-edit code -> resync preserves last-good
    )

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        # tree-sitter points are 0-based (row, col); expose 1-based inclusive line numbers.
        return node.start_point[0] + 1, node.end_point[0] + 1

    def field(node, name):
        return node.child_by_field_name(name) if node is not None else None

    def doc_for(inner, outer=None) -> str:
        """The leading doc comment of a declaration, cleaned. A comment (incl. JSDoc `/** */`,
        one block comment node) is a *preceding sibling* of the declaration -- but when the decl
        is wrapped (`export class ...`, `export const f = () => ...`) the comment sits above the
        OUTER `export_statement`, not the inner node, so fall back to it."""
        doc = leading_comment(inner, source)
        if not doc and outer is not None and outer is not inner:
            doc = leading_comment(outer, source)
        return doc

    def anon_types(node) -> set[str]:
        """The `.type` of every child (named or keyword) -- lets us detect the modifier tokens
        (readonly/static/abstract/get/set/?) that the grammar exposes as anonymous children."""
        return {c.type for c in node.children}

    def accessibility(node) -> str:
        for c in node.children:
            if c.type == "accessibility_modifier":
                return txt(c)
        return "public"

    def collect_type_refs(node) -> list[str]:
        """Every bare `type_identifier` name mentioned under a type node (annotation / alias
        value / heritage) -- predefined types (string/number/void/...) are a different node
        kind and so are naturally excluded. Generic params like `T` are collected too but get
        filtered downstream, where only names matching a project declaration become edges."""
        if node is None:
            return []
        out: list[str] = []
        seen: set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "type_identifier":
                name = txt(n)
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
            stack.extend(n.children)
        return out

    def annotation(type_annotation_node) -> tuple[str, list[str]]:
        """(text as written without the leading ':', bare type refs) for a `type_annotation`."""
        if type_annotation_node is None:
            return "", []
        raw = txt(type_annotation_node).lstrip()
        if raw.startswith(":"):
            raw = raw[1:]
        return " ".join(raw.split()), collect_type_refs(type_annotation_node)

    def simple_type_name(node) -> str | None:
        """The bare name a heritage/type node denotes: `Base` for `Base`, `Base<T>`, or
        `mod.Base`; None if it names nothing simple."""
        if node is None:
            return None
        if node.type in ("type_identifier", "identifier"):
            return txt(node)
        if node.type == "generic_type":
            return simple_type_name(field(node, "name"))
        if node.type in ("member_expression", "nested_type_identifier"):
            return simple_type_name(field(node, "property")) or _first_type_identifier(node)
        return _first_type_identifier(node)

    def _first_type_identifier(node) -> str | None:
        stack = [node]
        while stack:
            n = stack.pop(0)
            if n.type in ("type_identifier", "identifier"):
                return txt(n)
            stack.extend(n.children)
        return None

    def type_param_names(type_parameters_node) -> list[str]:
        if type_parameters_node is None:
            return []
        names = []
        for c in type_parameters_node.children:
            if c.type == "type_parameter":
                nm = field(c, "name")
                if nm is not None:
                    names.append(txt(nm))
        return names

    def _param_name(pattern) -> tuple[str, bool]:
        """(name, is_rest) for a parameter pattern; "" for a destructuring pattern (no simple
        name, but the positional slot is still recorded)."""
        if pattern is None:
            return "", False
        if pattern.type == "rest_pattern":
            return _first_type_identifier(pattern) or "", True
        if pattern.type == "identifier":
            return txt(pattern), False
        return "", False  # object_pattern / array_pattern

    def parameters(formal_parameters_node) -> list[RawParameter]:
        out: list[RawParameter] = []
        if formal_parameters_node is None:
            return out
        pos = 0
        for p in formal_parameters_node.children:
            if p.type not in ("required_parameter", "optional_parameter"):
                continue  # skips '(', ')', ','
            name, is_rest = _param_name(field(p, "pattern"))
            t_text, t_refs = annotation(field(p, "type"))
            optional = p.type == "optional_parameter" or field(p, "value") is not None
            out.append(
                RawParameter(
                    name=name, type=t_text, position=pos, optional=optional,
                    is_rest=is_rest, type_refs=t_refs,
                )
            )
            pos += 1
        return out

    def _callee(fn) -> tuple[str | None, str | None]:
        if fn is None:
            return None, None
        if fn.type == "identifier":
            return txt(fn), None
        if fn.type == "member_expression":
            prop = field(fn, "property")
            obj = field(fn, "object")
            recv = None
            if obj is not None and obj.type == "this":
                recv = "this"
            elif obj is not None and obj.type == "identifier":
                recv = txt(obj)
            return (txt(prop) if prop is not None else None), recv
        return None, None

    def _callee_node(fn):
        """The callee IDENTIFIER node SCIP anchors a call occurrence to: the `method` property of an
        `obj.method()`, else the callee itself (a bare `foo()` / `alias()`)."""
        if fn is not None and fn.type == "member_expression":
            prop = field(fn, "property")
            if prop is not None:
                return prop
        return fn

    def _callee_line(fn) -> int:
        """1-based line of the callee identifier -- for a multi-line `obj\n  .method()` that's the `method`
        line, so the deep pass's line-precise gate matches SCIP's occurrence line."""
        node = _callee_node(fn)
        return node.start_point[0] + 1 if node is not None else 0

    def _callee_col(fn) -> int:
        """0-based UTF-16 column of the callee identifier. scip-typescript emits occurrence columns in UTF-16
        code units (the TS/JS string unit), so the deep pass records call-site columns in the SAME encoding to
        compare directly against `Occurrence.start_char` (no conversion in the resolver). tree-sitter reports a
        BYTE column; for an all-ASCII file (the common case, incl. minified JS) that already IS the UTF-16
        column. Only a file containing non-ASCII pays a conversion, and that is cached PER LINE (storing just
        the non-ASCII boundaries), so many calls on one long minified line stay linear instead of each
        rescanning its prefix."""
        node = _callee_node(fn)
        if node is None:
            return 0
        byte_col = node.start_point[1]
        if byte_col == 0 or source_is_ascii:
            return byte_col  # all-ASCII file: byte column == UTF-16 column
        line_start = node.start_byte - byte_col
        entry = u16_cache.get(line_start)
        if entry is None:
            # Build once per line: (byte offset after each non-ASCII char, cumulative utf8-bytes-minus-utf16-
            # units delta). Then utf16_col(b) = b - delta_before(b) via bisect -- O(log k), k = non-ASCII chars.
            line_end = source.find(b"\n", line_start)
            line = source[line_start : len(source) if line_end == -1 else line_end].decode("utf-8", "replace")
            bounds: list[int] = []
            cum: list[int] = []
            b = delta = 0
            for ch in line:
                blen = len(ch.encode("utf-8"))
                if blen > 1:  # non-ASCII: 1 UTF-16 unit (BMP) or 2 (astral); ASCII (blen 1) adds no delta
                    delta += blen - (2 if ord(ch) > 0xFFFF else 1)
                    bounds.append(b + blen)
                    cum.append(delta)
                b += blen
            entry = (bounds, cum)
            u16_cache[line_start] = entry
        bounds, cum = entry
        i = bisect_right(bounds, byte_col)
        return byte_col - (cum[i - 1] if i else 0)

    def _route_text(node) -> str:
        """URL text of a string / template-string arg, with `${...}` interpolations rendered as
        `{param}` so a templated URL matches an endpoint route template (`api/x/${id}` -> api/x/{param})."""
        if node.type == "template_string":
            parts = []
            for c in node.children:
                if c.type == "string_fragment":
                    parts.append(txt(c))
                elif c.type == "template_substitution":
                    parts.append("{param}")
            return "".join(parts)
        return _string_literal(node)

    def _http_request(call_node, name: str) -> tuple[str | None, str | None]:
        """(url, method) for an HTTP-client call -- best-effort, only invoked when the callee is
        a known HTTP idiom. url = first string arg or an object arg's `url:`; method = the call's
        verb (get/post/…) or an object arg's `method:`/`type:`; (None, None) if no url is found."""
        args = field(call_node, "arguments")
        if args is None:
            return None, None
        method = _HTTP_VERB_TO_METHOD.get(name.lower())
        url = None
        for a in args.children:
            if a.type in (",", "(", ")"):
                continue
            if url is None and a.type in ("string", "template_string"):
                url = _route_text(a)
            elif a.type == "object":
                for prop in a.children:
                    if prop.type != "pair":
                        continue
                    key = txt(field(prop, "key")).strip("\"'`")
                    val = field(prop, "value")
                    if val is None or val.type not in ("string", "template_string"):
                        continue
                    if key == "url" and url is None:
                        url = _route_text(val)
                    elif key in ("method", "type") and method is None:
                        method = _string_literal(val).upper()
        return (url or None), method

    def _configured_client_vars() -> frozenset[str]:
        """Names bound to a configured HTTP client -- `const api = axios.create(...)` -- so a later
        `api.get(...)` counts as HTTP. Precise (only axios.create), so no false positive on any x.get()."""
        names: set[str] = set()
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type == "variable_declarator":
                name_node, value = field(n, "name"), field(n, "value")
                if (name_node is not None and name_node.type == "identifier"
                        and value is not None and value.type == "call_expression"):
                    callee = field(value, "function")
                    if (callee is not None and callee.type == "member_expression"
                            and txt(field(callee, "object")) == "axios"
                            and txt(field(callee, "property")) == "create"):
                        names.add(txt(name_node))
            stack.extend(n.children)
        return frozenset(names)

    http_client_vars = _configured_client_vars()

    def collect_calls(body) -> list:
        if body is None:
            return []
        out: list = []
        seen: set = set()
        # Start from the body node itself, not its children: an expression-bodied arrow
        # (`() => make(s)`) has the call_expression *as* the body, so descending straight into
        # children would skip the outermost call.
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type in ("call_expression", "new_expression"):
                is_new = n.type == "new_expression"
                callee = field(n, "constructor" if is_new else "function")
                # Unwrap callee wrappers that are transparent to the call target, so the call site is
                # recorded (and SCIP's occurrence, anchored at the inner identifier, matches): `(b.run)()`,
                # `b.run!()`, `(f as T)()`, `(f satisfies T)()`, `(<T>f)()` (incl. nesting). Pick the
                # expression child (not a type argument, which may come first in `as`/`<T>` forms).
                while callee is not None and callee.type in _TRANSPARENT_CALLEES:
                    callee = next((c for c in callee.named_children if c.type in _CALLEE_EXPR_NODES), None)
                nm, recv = _callee(callee)
                if nm:
                    line = _callee_line(callee)
                    col = _callee_col(callee)
                    # Dedup by (line, col) too: the deep pass's call-site gate needs EVERY site's position, so
                    # `x.run()` on two lines -- or two same-name calls on ONE line (`a.run(); b.run()`) -- must
                    # each be kept (a name-only key would drop the second). Same position is still one call.
                    key = (nm, recv, is_new, line, col)
                    if key not in seen:
                        seen.add(key)
                        http_url = http_method = None
                        if not is_new and _is_http_idiom(nm, recv, http_client_vars):  # gate the arg walk (perf)
                            http_url, http_method = _http_request(n, nm)
                        out.append(RawCall(name=nm, receiver=recv, is_new=is_new, http_url=http_url,
                                           http_method=http_method, line=line, col=col))
            stack.extend(n.children)
        return out

    def method_from(node) -> RawMethod:
        """From a `method_definition` (class) or `method_signature` (interface)."""
        name = txt(field(node, "name"))
        types = anon_types(node)
        if name == "constructor":
            kind = "CONSTRUCTOR"
        elif "get" in types:
            kind = "GETTER"
        elif "set" in types:
            kind = "SETTER"
        else:
            kind = "METHOD"
        ret_text, ret_refs = annotation(field(node, "return_type"))
        body = field(node, "body")
        ls, le = lines(node)
        return RawMethod(
            name=name, kind=kind, accessibility=accessibility(node),
            is_static="static" in types, is_abstract="abstract" in types,
            is_async="async" in types, params=parameters(field(node, "parameters")),
            return_type=ret_text, return_type_refs=ret_refs,
            calls=collect_calls(body), source=txt(node), doc=doc_for(node),
            line_start=ls, line_end=le,
        )

    def property_from(node) -> RawProperty:
        """From a `public_field_definition` (class) or `property_signature` (interface)."""
        types = anon_types(node)
        t_text, t_refs = annotation(field(node, "type"))
        return RawProperty(
            name=txt(field(node, "name")), type=t_text, type_refs=t_refs,
            accessibility=accessibility(node), is_static="static" in types,
            is_readonly="readonly" in types, is_optional="?" in types,
        )

    def property_from_param(param_node) -> RawProperty:
        """A TS "parameter property": `constructor(private readonly store: Store)` declares a
        class field as a side effect of the parameter."""
        name, _ = _param_name(field(param_node, "pattern"))
        t_text, t_refs = annotation(field(param_node, "type"))
        types = anon_types(param_node)
        return RawProperty(
            name=name, type=t_text, type_refs=t_refs, accessibility=accessibility(param_node),
            is_readonly="readonly" in types, is_optional=param_node.type == "optional_parameter",
        )

    def _param_is_property(param_node) -> bool:
        return any(c.type in ("accessibility_modifier", "readonly") for c in param_node.children)

    def class_from(node, exported: bool, doc: str = "") -> RawClass:
        types = anon_types(node)
        extends: list[str] = []
        implements: list[str] = []
        for h in node.children:
            if h.type != "class_heritage":
                continue
            for clause in h.children:
                if clause.type == "extends_clause":
                    nm = simple_type_name(field(clause, "value"))
                    if nm:
                        extends.append(nm)
                elif clause.type == "implements_clause":
                    for it in clause.children:
                        if it.type in ("type_identifier", "generic_type"):
                            nm = simple_type_name(it)
                            if nm:
                                implements.append(nm)
        methods: list[RawMethod] = []
        properties: list[RawProperty] = []
        body = field(node, "body")
        if body is not None:
            for m in body.children:
                if m.type == "method_definition":
                    meth = method_from(m)
                    methods.append(meth)
                    if meth.kind == "CONSTRUCTOR":
                        fp = field(m, "parameters")
                        if fp is not None:
                            properties.extend(
                                property_from_param(pn)
                                for pn in fp.children
                                if pn.type in ("required_parameter", "optional_parameter")
                                and _param_is_property(pn)
                            )
                elif m.type == "public_field_definition":
                    properties.append(property_from(m))
        ls, le = lines(node)
        return RawClass(
            name=txt(field(node, "name")),
            is_abstract=node.type == "abstract_class_declaration" or "abstract" in types,
            is_exported=exported, type_parameters=type_param_names(field(node, "type_parameters")),
            extends=extends, implements=implements, methods=methods, properties=properties,
            source=txt(node), doc=doc, line_start=ls, line_end=le,
        )

    def interface_from(node, exported: bool, doc: str = "") -> RawInterface:
        extends: list[str] = []
        for c in node.children:
            if c.type == "extends_type_clause":
                for it in c.children:
                    if it.type in ("type_identifier", "generic_type"):
                        nm = simple_type_name(it)
                        if nm:
                            extends.append(nm)
        methods: list[RawMethod] = []
        properties: list[RawProperty] = []
        body = field(node, "body")
        if body is not None:
            for m in body.children:
                if m.type == "method_signature":
                    methods.append(method_from(m))
                elif m.type == "property_signature":
                    properties.append(property_from(m))
        ls, le = lines(node)
        return RawInterface(
            name=txt(field(node, "name")), is_exported=exported,
            type_parameters=type_param_names(field(node, "type_parameters")),
            extends=extends, methods=methods, properties=properties,
            source=txt(node), doc=doc, line_start=ls, line_end=le,
        )

    def enum_from(node, exported: bool, doc: str = "") -> RawEnum:
        members: list[str] = []
        body = field(node, "body")
        if body is not None:
            for e in body.children:
                if e.type == "property_identifier":
                    members.append(txt(e))
                elif e.type == "enum_assignment":
                    members.append(txt(field(e, "name")))
        ls, le = lines(node)
        return RawEnum(
            name=txt(field(node, "name")), is_const="const" in anon_types(node),
            is_exported=exported, members=members, source=txt(node), doc=doc,
            line_start=ls, line_end=le,
        )

    def type_alias_from(node, exported: bool, doc: str = "") -> RawTypeAlias:
        value = field(node, "value")
        ls, le = lines(node)
        return RawTypeAlias(
            name=txt(field(node, "name")), value=" ".join(txt(value).split()),
            is_exported=exported, type_refs=collect_type_refs(value),
            source=txt(node), doc=doc, line_start=ls, line_end=le,
        )

    def function_from(node, exported: bool, kind: str, name: str, doc: str = "") -> RawFunction:
        ret_text, ret_refs = annotation(field(node, "return_type"))
        body = field(node, "body")
        ls, le = lines(node)
        return RawFunction(
            name=name, kind=kind, is_exported=exported, is_async="async" in anon_types(node),
            params=parameters(field(node, "parameters")), return_type=ret_text,
            return_type_refs=ret_refs, calls=collect_calls(body), source=txt(node),
            doc=doc, line_start=ls, line_end=le,
        )

    def _string_literal(node) -> str:
        """The specifier text of a `string` node, quotes stripped."""
        if node is None:
            return ""
        for c in node.children:
            if c.type == "string_fragment":
                return txt(c)
        return txt(node).strip("\"'`")

    def import_from(node) -> RawImport | None:
        src = _string_literal(field(node, "source"))
        if not src:
            return None
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            return RawImport(module=src, kind="side_effect", line_start=lines(node)[0])
        names: list[str] = []
        kind = "named"
        for c in clause.children:
            if c.type == "identifier":  # default import
                kind = "default" if not names else kind
                names.append(txt(c))
            elif c.type == "namespace_import":
                kind = "namespace"
                names.append(_first_type_identifier(c) or "")
            elif c.type == "named_imports":
                for spec in c.children:
                    if spec.type == "import_specifier":
                        alias = field(spec, "alias")
                        names.append(txt(alias) if alias is not None else txt(field(spec, "name")))
        return RawImport(module=src, kind=kind, names=names, line_start=lines(node)[0])

    def reexport_from(node) -> RawImport | None:
        """`export { A } from "m"` / `export * from "m"` -- a Module->Module dependency."""
        src = _string_literal(field(node, "source"))
        if not src:
            return None
        names: list[str] = ["*"]
        clause = next((c for c in node.children if c.type == "export_clause"), None)
        if clause is not None:
            names = []
            for spec in clause.children:
                if spec.type == "export_specifier":
                    alias = field(spec, "alias")
                    names.append(txt(alias) if alias is not None else txt(field(spec, "name")))
        return RawImport(module=src, kind="reexport", names=names, line_start=lines(node)[0])

    def dispatch_declaration(decl, exported: bool, outer=None) -> None:
        # A comment above `export class X` / `export const f = ...` attaches to the OUTER
        # export_statement, so `outer` (the wrapper, or None when the decl is bare) is consulted.
        doc = doc_for(decl, outer)
        if decl.type in _CLASS_DECLS:
            rf.classes.append(class_from(decl, exported, doc))
        elif decl.type == "interface_declaration":
            rf.interfaces.append(interface_from(decl, exported, doc))
        elif decl.type == "enum_declaration":
            rf.enums.append(enum_from(decl, exported, doc))
        elif decl.type == "type_alias_declaration":
            rf.type_aliases.append(type_alias_from(decl, exported, doc))
        elif decl.type == "function_declaration":
            name = txt(field(decl, "name"))
            if name:
                rf.functions.append(function_from(decl, exported, "FUNCTION", name, doc))
        elif decl.type in _LEXICAL:
            _lexical_functions(decl, exported, doc)

    def _lexical_functions(decl, exported: bool, doc: str = "") -> None:
        """`const f = () => ...` / `const g = function () {}` -> a module-level Function. Plain
        value bindings (`const x = 42`) are deferred (no Variable node in v1). The doc comment
        sits above the `lexical_declaration` (or its export wrapper), never the arrow/function
        value node, so it is threaded in from `dispatch_declaration`."""
        for d in decl.children:
            if d.type != "variable_declarator":
                continue
            value = field(d, "value")
            if value is not None and value.type in _FUNC_VALUE_KINDS:
                name = txt(field(d, "name"))
                if name:
                    rf.functions.append(
                        function_from(value, exported, _FUNC_VALUE_KINDS[value.type], name, doc)
                    )

    for child in tree.root_node.children:
        if child.type == "import_statement":
            imp = import_from(child)
            if imp is not None:
                rf.imports.append(imp)
        elif child.type == "export_statement":
            decl = field(child, "declaration")
            if decl is not None:
                dispatch_declaration(decl, exported=True, outer=child)
            elif field(child, "source") is not None:  # re-export
                imp = reexport_from(child)
                if imp is not None:
                    rf.imports.append(imp)
        else:
            dispatch_declaration(child, exported=False)

    return rf
