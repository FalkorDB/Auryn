"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned,
no tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-julia 0.23.1
(see the package's grammar probe), not assumed -- Julia's node names are less obvious than the
C-family grammars:

- top level / module body: children of `source_file` (project scope) or of a `module_definition`
  (module scope). `module_definition` has a `name:` field (identifier); its body statements are
  its direct children (there is NO separate body node); submodules are nested `module_definition`.
- types: `struct_definition` (optional anon `mutable` token; a `type_head`; fields as direct
  `typed_expression`/bare `identifier` children; inner constructors as `assignment`/
  `function_definition` children -- skipped for field extraction), `abstract_definition`
  (`type_head`), `primitive_definition` (`type_head` + `integer_literal` bit-count).
- `type_head` holds one of: `identifier` (bare name); `parametrized_type_expression`
  (identifier + `curly_expression` of type-var identifiers); or `binary_expression`
  (name-expr, `operator` `<:`, supertype-expr) for `Name <: Super`.
- functions: long form `function_definition` (anon `function`; a `signature`; body = the other
  direct children; anon `end`) and short form `assignment` whose LHS, after peeling
  `where_expression`/`typed_expression` wrappers, is a `call_expression`. A `signature` /
  short-LHS wraps as: [`where_expression` (`where {T,S}` or `where T`)] -> [`typed_expression`
  (`::ReturnType`)] -> `call_expression` (callee `identifier`/`field_expression`, `argument_list`).
- parameters (children of `argument_list`): `typed_expression` (`x::T`), bare `identifier`
  (untyped), `unary_typed_expression` (`::T`, anonymous dispatch param), `named_argument`
  (keyword arg after `;`, or optional positional), `splat_expression` (`rest...`/`rest::T...`).
- calls: `call_expression`; callee is the first named child -- an `identifier` (`foo`) or a
  `field_expression` (`Base.show` / `obj.method`, whose last identifier is the method name and
  whose `value:` field, when a bare identifier, is the receiver). `field_expression` (`c.radius`)
  is field access.
- imports: `using_statement` / `import_statement`, each holding a module `identifier` or a
  `selected_import` (`Base: show, length`).
- docstrings: Julia's canonical doc (the `@doc` convention) is a `string_literal` placed
  immediately ABOVE a declaration -- it parses as the declaration's previous *named* sibling, a
  `string_literal` whose text lives in `content` child(ren) between the quote tokens. It is a
  string, NOT a comment, so `core.docstrings.leading_comment` (which only walks comment siblings)
  never catches it; `julia_doc` handles it, preferring the string docstring over a `#` comment.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import clean_comment, leading_comment
from auryn.ingestors.julia.grammar import new_parser
from auryn.ingestors.julia.raw_model import (
    RawCall,
    RawField,
    RawFile,
    RawMethod,
    RawModule,
    RawParameter,
    RawType,
    RawUsing,
)

_TYPE_DECL_KINDS = {
    "struct_definition": "Struct",
    "abstract_definition": "AbstractType",
    "primitive_definition": "PrimitiveType",
}
_SIGNATURE_WRAPPERS = ("where_expression", "typed_expression")
_PARAM_NODE_TYPES = (
    "typed_expression",
    "identifier",
    "unary_typed_expression",
    "named_argument",
    "splat_expression",
)


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)
    rf = RawFile(path=path_label)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def lines(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def string_doc_text(str_node) -> str:
        """Inner prose of a docstring `string_literal`, with the surrounding quote delimiters
        (`\"\"\"...\"\"\"` or `\"...\"`) stripped; interpolation/escapes are kept as written."""
        raw = txt(str_node)
        for q in ('"""', '"'):
            if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                return raw[len(q):-len(q)]
        return raw

    def julia_doc(node) -> str:
        """The leading documentation attached to a declaration `node`, cleaned, or "".

        Julia's canonical doc is a string literal placed immediately above the declaration (the
        `@doc` convention): it parses as the declaration's previous *named* sibling -- a
        `string_literal`, NOT a comment, so `leading_comment` (which only walks comment siblings)
        never sees it. Prefer that string docstring when the previous named sibling is a string
        literal vertically adjacent to the declaration (same blank-line-gap rule as comments);
        otherwise fall back to a `#` comment block."""
        prev = node.prev_named_sibling
        if (prev is not None and "string" in prev.type
                and node.start_point[0] - prev.end_point[0] <= 1):
            return clean_comment(string_doc_text(prev))
        return leading_comment(node, source)

    def simple_name(node) -> str:
        """The bare simple name an expression denotes: `Base` -> Base, `Foo{T}` -> Foo (base of
        a parametrized_type_expression), `Base.Show` -> Show (last segment of a field_expression),
        else the node's own text."""
        if node is None:
            return ""
        if node.type == "identifier":
            return txt(node)
        if node.type == "parametrized_type_expression":
            return simple_name(node.named_children[0]) if node.named_children else ""
        if node.type == "field_expression":
            idents = [c for c in node.named_children if c.type == "identifier"]
            return txt(idents[-1]) if idents else ""
        # descend to the first identifier we can find
        stack = list(node.named_children)
        while stack:
            n = stack.pop(0)
            if n.type == "identifier":
                return txt(n)
            stack.extend(n.named_children)
        return ""

    def curly_typevars(curly_node) -> list[str]:
        return [txt(c) for c in curly_node.named_children if c.type == "identifier"]

    def name_and_typevars(expr) -> tuple[str, list[str]]:
        """From a type_head's name-expression: (name, type-var names)."""
        if expr is None:
            return "", []
        if expr.type == "parametrized_type_expression":
            nc = expr.named_children
            name = simple_name(nc[0]) if nc else ""
            curly = next((c for c in nc if c.type == "curly_expression"), None)
            return name, (curly_typevars(curly) if curly is not None else [])
        return simple_name(expr), []

    def parse_type_head(head) -> tuple[str, list[str], str | None]:
        """(name, type-vars, supertype-name) from a `type_head` node."""
        if head is None:
            return "", [], None
        inner = head.named_children[0] if head.named_children else None
        if inner is None:
            return "", [], None
        if inner.type == "binary_expression":
            nc = inner.named_children  # [name-expr, operator?, supertype-expr]
            op = next((c for c in inner.children if c.type == "operator"), None)
            name, tvs = name_and_typevars(nc[0]) if nc else ("", [])
            supertype = simple_name(nc[-1]) if (op is not None and txt(op) == "<:" and len(nc) >= 2) else None
            return name, tvs, supertype
        name, tvs = name_and_typevars(inner)
        return name, tvs, None

    def build_struct_fields(struct_node) -> list[RawField]:
        out: list[RawField] = []
        for c in struct_node.named_children:
            if c.type == "typed_expression":
                nc = c.named_children
                name = txt(nc[0]) if nc and nc[0].type == "identifier" else ""
                ftype = type_text(nc[1]) if len(nc) >= 2 else ""
                ls, le = lines(c)
                out.append(RawField(name=name, type=ftype, line_start=ls, line_end=le))
            elif c.type == "identifier":  # bare (untyped) field
                ls, le = lines(c)
                out.append(RawField(name=txt(c), type="", line_start=ls, line_end=le))
            # else: type_head, inner constructors (assignment/function_definition), etc. -- not fields
        return out

    def build_type(node) -> RawType:
        kind = _TYPE_DECL_KINDS[node.type]
        head = next((c for c in node.named_children if c.type == "type_head"), None)
        name, tvs, supertype = parse_type_head(head)
        ls, le = lines(node)
        rt = RawType(
            kind=kind, name=name, type_parameters=tvs, supertype=supertype,
            source=txt(node), doc=julia_doc(node), line_start=ls, line_end=le,
        )
        if kind == "Struct":
            rt.is_mutable = any(c.type == "mutable" for c in node.children)
            rt.fields = build_struct_fields(node)
        elif kind == "PrimitiveType":
            lit = next((c for c in node.named_children if c.type == "integer_literal"), None)
            if lit is not None:
                try:
                    rt.bits = int(txt(lit))
                except ValueError:
                    rt.bits = None
        return rt

    def where_typevars(where_node) -> list[str]:
        # named_children = [inner-expr, typevar(s)...]; a typevar is a curly_expression or identifier
        out: list[str] = []
        for c in where_node.named_children[1:]:
            if c.type == "curly_expression":
                out.extend(curly_typevars(c))
            elif c.type == "identifier":
                out.append(txt(c))
        return out

    def unwrap_signature(node) -> tuple[object, str | None, list[str]]:
        """Peel `where_expression`/`typed_expression` wrappers off a signature (or a short-form
        LHS), returning (call_expression_node_or_None, return_type_text, type_var_names)."""
        return_type: str | None = None
        type_params: list[str] = []
        while node is not None and node.type in _SIGNATURE_WRAPPERS:
            if node.type == "where_expression":
                type_params.extend(where_typevars(node))
                node = node.named_children[0] if node.named_children else None
            else:  # typed_expression: [value, type]
                nc = node.named_children
                if len(nc) >= 2:
                    return_type = type_text(nc[1])
                node = nc[0] if nc else None
        if node is not None and node.type == "call_expression":
            return node, return_type, type_params
        return None, return_type, type_params

    def build_param(node, position: int) -> RawParameter | None:
        if node.type == "identifier":
            return RawParameter(name=txt(node), type="", position=position)
        if node.type == "typed_expression":
            nc = node.named_children
            name = txt(nc[0]) if nc and nc[0].type == "identifier" else ""
            ptype = type_text(nc[1]) if len(nc) >= 2 else ""
            return RawParameter(name=name, type=ptype, position=position)
        if node.type == "unary_typed_expression":  # `::T` anonymous dispatch param
            nc = node.named_children
            return RawParameter(name="", type=type_text(nc[-1]) if nc else "", position=position)
        if node.type == "named_argument":  # keyword arg (after `;`) or optional positional
            inner = node.named_children[0] if node.named_children else None
            base = build_param(inner, position) if inner is not None else None
            if base is None:
                return None
            base.is_keyword = True
            return base
        if node.type == "splat_expression":  # `rest...` / `rest::T...`
            inner = node.named_children[0] if node.named_children else None
            base = build_param(inner, position) if inner is not None else RawParameter("", "", position)
            base.is_vararg = True
            return base
        return None

    def parse_parameters(call_node) -> list[RawParameter]:
        arglist = next((c for c in call_node.named_children if c.type == "argument_list"), None)
        if arglist is None:
            return []
        out: list[RawParameter] = []
        pos = 0
        for c in arglist.named_children:
            if c.type in _PARAM_NODE_TYPES:
                p = build_param(c, pos)
                if p is not None:
                    out.append(p)
                    pos += 1
        return out

    def callee_name_receiver(call_node) -> tuple[str, str | None]:
        callee = call_node.named_children[0] if call_node.named_children else None
        if callee is None:
            return "", None
        if callee.type == "identifier":
            return txt(callee), None
        if callee.type == "field_expression":
            value = callee.child_by_field_name("value")
            recv = txt(value) if value is not None and value.type == "identifier" else None
            return simple_name(callee), recv
        return simple_name(callee), None

    def collect_calls(body_nodes) -> list[RawCall]:
        out: list[RawCall] = []
        seen: set = set()
        stack = list(body_nodes)
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                nm, recv = callee_name_receiver(n)
                key = (nm, recv)
                if nm and nm != "new" and key not in seen:
                    seen.add(key)
                    out.append(RawCall(name=nm, receiver=recv))
            stack.extend(n.children)
        return out

    def build_method_from_signature(fn_node, signature_node) -> RawMethod | None:
        call, return_type, type_params = unwrap_signature(
            signature_node.named_children[0] if signature_node.named_children else None
        )
        if call is None:
            return None
        name, _ = callee_name_receiver(call)
        if not name:
            return None
        body = [c for c in fn_node.named_children if c is not signature_node]
        ls, le = lines(fn_node)
        return RawMethod(
            name=name, parameters=parse_parameters(call), return_type=return_type,
            type_parameters=type_params, is_short=False, calls=collect_calls(body),
            source=txt(fn_node), doc=julia_doc(fn_node), line_start=ls, line_end=le,
        )

    def build_method_from_assignment(assign_node) -> RawMethod | None:
        nc = assign_node.named_children
        if not nc:
            return None
        call, return_type, type_params = unwrap_signature(nc[0])
        if call is None:  # LHS is not a call -> a variable assignment, not a function definition
            return None
        name, _ = callee_name_receiver(call)
        if not name:
            return None
        rhs = nc[-1] if len(nc) >= 2 else None
        ls, le = lines(assign_node)
        return RawMethod(
            name=name, parameters=parse_parameters(call), return_type=return_type,
            type_parameters=type_params, is_short=True,
            calls=collect_calls([rhs]) if rhs is not None else [],
            source=txt(assign_node), doc=julia_doc(assign_node), line_start=ls, line_end=le,
        )

    def build_using(node, is_import: bool) -> RawUsing:
        selected = next((c for c in node.named_children if c.type == "selected_import"), None)
        if selected is not None:
            idents = [c for c in selected.named_children if c.type == "identifier"]
            module = txt(idents[0]) if idents else ""
            names = [txt(i) for i in idents[1:]]
            return RawUsing(module=module, names=names, is_import=is_import)
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        return RawUsing(module=txt(ident), names=[], is_import=is_import)

    def parse_scope(body_nodes, container) -> None:
        """Walk a scope's statements (source_file children, or a module_definition's body),
        filling `container` (a RawFile or RawModule) and appending usings to the file."""
        for child in body_nodes:
            t = child.type
            if t in _TYPE_DECL_KINDS:
                container.types.append(build_type(child))
            elif t == "module_definition":
                container.submodules.append(build_module(child)) if isinstance(container, RawModule) \
                    else container.modules.append(build_module(child))
            elif t == "function_definition":
                sig = next((c for c in child.named_children if c.type == "signature"), None)
                if sig is not None:
                    m = build_method_from_signature(child, sig)
                    if m is not None:
                        container.functions.append(m)
            elif t == "assignment":
                m = build_method_from_assignment(child)
                if m is not None:
                    container.functions.append(m)
            elif t == "const_statement":  # `const f(x) = ...` is legal, though rare
                inner = next((c for c in child.named_children if c.type == "assignment"), None)
                if inner is not None:
                    m = build_method_from_assignment(inner)
                    if m is not None:
                        container.functions.append(m)
            elif t == "using_statement":
                rf.usings.append(build_using(child, is_import=False))
            elif t == "import_statement":
                rf.usings.append(build_using(child, is_import=True))

    def build_module(node) -> RawModule:
        name_node = node.child_by_field_name("name")
        ls, le = lines(node)
        rm = RawModule(name=txt(name_node), source=txt(node), doc=julia_doc(node),
                       line_start=ls, line_end=le)
        body = [c for c in node.named_children if c is not name_node]
        parse_scope(body, rm)
        return rm

    parse_scope(list(tree.root_node.children), rf)
    return rf
