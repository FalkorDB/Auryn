"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared state
-- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned, no
tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-rust 0.24.2
(see this package's grammar probe), not assumed:

- items (children of `source_file`, or of a `mod_item`'s `declaration_list` body):
  `mod_item` (`name` ident, optional `body` declaration_list -- absent for external `mod foo;`),
  `struct_item` (`name` type_identifier, optional `type_parameters`, `body` =
  `field_declaration_list` named / `ordered_field_declaration_list` tuple / absent unit),
  `enum_item` (`body` = `enum_variant_list` of `enum_variant`s, each `name` + optional `body`),
  `trait_item` (`name`, optional `bounds` = `trait_bounds`, `body` = `declaration_list`),
  `impl_item` (`type` field = implementing type, optional `trait` field, optional
  `type_parameters`, `body` = `declaration_list`),
  `function_item` (`name`, `parameters`, optional `return_type`, `body` block) and
  `function_signature_item` (trait required method, no body),
  `const_item`/`static_item` (`name`, `type`, `value`), `use_declaration` (`argument`).
- `visibility_modifier` is a named child (no field name) -> `pub`/`pub(crate)`/...;
  `function_modifiers` holds async/unsafe/const; `#[...]` are sibling `attribute_item` nodes.
- `parameters` -> `self_parameter` (a receiver: `self` / `&self` / `&mut self`, no field names)
  and `parameter` (`pattern` field = binding, `type` field). type nodes seen: `type_identifier`,
  `primitive_type`, `generic_type` (`type`+`type_arguments`), `scoped_type_identifier`
  (`path`+`name`), `reference_type` (`&`,`mutable_specifier`?,`type`).
- calls: `call_expression` (`function`+`arguments`); the `function` is an `identifier` (bare
  `foo()` -> "free"), a `field_expression` (`value`+`field` -> `x.m()`/`self.m()` -> "method"),
  or a `scoped_identifier` (`path`+`name` -> `Type::assoc()` / `mod::func()` -> "path").
  `struct_expression` (a `Point { .. }` literal) and `macro_invocation` are NOT calls.
"""

from __future__ import annotations

import re
from pathlib import Path

from auryn.core.docstrings import clean_comment

# A call-like site `ident(` -- an identifier immediately followed by `(`. Applied to a macro
# invocation's text to count the calls inside it the name pass can't see (the macro name itself is
# `name!(`, so the `!` keeps it from matching). A variant constructor (`Some(`) counts too -- a
# conservative over-count is the safe direction for an "answer may be incomplete" signal (#792).
_MACRO_CALL_RE = re.compile(r"[A-Za-z_]\w*\s*\(")
from auryn.ingestors.rust.grammar import new_parser
from auryn.ingestors.rust.raw_model import (
    RawCall,
    RawConst,
    RawField,
    RawFile,
    RawFunction,
    RawImpl,
    RawModule,
    RawParameter,
    RawType,
    RawVariant,
)

_TYPE_ITEMS = {"struct_item": "Struct", "enum_item": "Enum", "trait_item": "Trait"}


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def fieldn(node, name):
        return node.child_by_field_name(name) if node is not None else None

    def type_text(node) -> str:
        return " ".join(txt(node).split()) if node is not None else ""

    def visibility(node) -> str:
        vm = next((c for c in node.children if c.type == "visibility_modifier"), None)
        return " ".join(txt(vm).split()) if vm is not None else "private"

    def modifiers(node) -> list[str]:
        fm = next((c for c in node.children if c.type == "function_modifiers"), None)
        return txt(fm).split() if fm is not None else []

    def doc_of(node) -> str:
        """The shared `doc`: the leading comment above a declaration, cleaned via
        `core.docstrings.clean_comment` (see docs/graph-model.md).

        This walks a declaration's preceding *named* comment siblings itself rather than calling
        `core.docstrings.leading_comment`, because tree-sitter-rust's comment nodes have two
        traits that helper (calibrated for tree-sitter-go's newline-*excluding* nodes) mishandles:

        * `#[...]` attributes are `attribute_item` *siblings* that sit between a doc comment and
          the item it decorates -- so we first step back over the attribute stack to its top.
        * a `///`/`//!` doc comment's `line_comment` node *includes its trailing newline* in its
          byte span and end point (a plain `//` node does not). Left as-is that both doubles the
          blank line `"\n".join` inserts between stacked `///` lines and shifts the blank-line-gap
          test off by one. We `rstrip("\n")` each span before joining and measure the gap against
          the comment's last *content* row, so a Rust doc reads identically to every other
          ingestor's (cross-ingestor `doc` contract) and a blank line still detaches the comment."""
        anchor = node
        while True:
            prev = anchor.prev_named_sibling
            if prev is None or prev.type != "attribute_item":
                break
            anchor = prev
        comments = []
        cur = anchor
        while True:
            prev = cur.prev_named_sibling
            if prev is None or "comment" not in prev.type:
                break
            span = source[prev.start_byte : prev.end_byte]
            last_row = prev.end_point[0] - (1 if span.endswith(b"\n") else 0)
            if cur.start_point[0] - last_row > 1:  # blank line between -> not a doc comment
                break
            comments.append(prev)
            cur = prev
        if not comments:
            return ""
        comments.reverse()
        raw = "\n".join(
            source[c.start_byte : c.end_byte].decode("utf-8", "replace").rstrip("\n")
            for c in comments
        )
        return clean_comment(raw)

    def simple_type_name(node) -> str | None:
        """The bare simple name a type node denotes: `Base` for `Base`, `Base<T>` (generic_type),
        `pkg::Base` (scoped_type_identifier), `&mut Base` (reference_type). None if it names none."""
        if node is None:
            return None
        t = node.type
        if t in ("type_identifier", "primitive_type"):
            return txt(node)
        if t in ("generic_type", "reference_type"):
            return simple_type_name(fieldn(node, "type"))
        if t == "scoped_type_identifier":
            name = fieldn(node, "name")
            return txt(name) if name is not None else None
        stack = list(node.children)
        while stack:
            n = stack.pop(0)
            if n.type == "type_identifier":
                return txt(n)
            stack.extend(n.children)
        return None

    def type_parameters(node) -> list[str]:
        tp = fieldn(node, "type_parameters")
        if tp is None:
            return []
        out = []
        for c in tp.named_children:
            if c.type == "type_parameter":
                ident = fieldn(c, "name") or next(
                    (g for g in c.children if g.type == "type_identifier"), None
                )
                if ident is not None:
                    out.append(txt(ident))
        return out

    def parameters(params_node) -> tuple[list[RawParameter], str | None]:
        """(non-self parameters, self_kind) where self_kind is value/ref/mut_ref or None."""
        if params_node is None:
            return [], None
        out: list[RawParameter] = []
        self_kind: str | None = None
        pos = 0
        for c in params_node.named_children:
            if c.type == "self_parameter":
                has_ref = any(g.type == "&" for g in c.children)
                has_mut = any(g.type == "mutable_specifier" for g in c.children)
                self_kind = "mut_ref" if (has_ref and has_mut) else "ref" if has_ref else "value"
            elif c.type == "parameter":
                name = txt(fieldn(c, "pattern"))
                if name.startswith("mut "):
                    name = name[4:]
                out.append(RawParameter(name=name, type=type_text(fieldn(c, "type")), position=pos))
                pos += 1
            elif c.type == "variadic_parameter":  # `...` in extern fns
                out.append(RawParameter(name="...", type="", position=pos))
                pos += 1
        return out, self_kind

    def _extract_callee(fn) -> RawCall | None:
        if fn is None:
            return None
        t = fn.type
        if t == "identifier":
            return RawCall(name=txt(fn), kind="free")
        if t == "field_expression":
            method = fieldn(fn, "field")
            if method is None:
                return None
            val = fieldn(fn, "value")
            recv = None
            if val is not None and val.type == "self":
                recv = "self"
            elif val is not None and val.type == "identifier":
                recv = txt(val)
            elif val is not None and val.type == "field_expression":
                inner = fieldn(val, "field")  # `self.store.m()` -> receiver key "store"
                recv = txt(inner) if inner is not None else None
            return RawCall(name=txt(method), kind="method", receiver=recv)
        if t == "scoped_identifier":
            name_node = fieldn(fn, "name")
            if name_node is None:
                return None
            path = fieldn(fn, "path")
            qualifier = None
            full_path = txt(path) if path is not None else None  # "std::mem" for std::mem::swap
            if path is not None:
                if path.type in ("identifier", "crate", "self", "super", "metavariable"):
                    qualifier = txt(path)
                elif path.type == "scoped_identifier":
                    qualifier = txt(fieldn(path, "name"))
            return RawCall(name=txt(name_node), kind="path", qualifier=qualifier, path=full_path)
        if t == "generic_function":  # `foo::<T>()` / `Type::method::<T>()`
            return _extract_callee(fieldn(fn, "function"))
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
                call = _extract_callee(fieldn(n, "function"))
                if call is not None and call.name:
                    key = (call.name, call.kind, call.receiver, call.qualifier, call.path)
                    if key not in seen:
                        seen.add(key)
                        out.append(call)
            stack.extend(n.children)
        return out

    def count_macro_calls(body) -> int:
        """Number of call-like sites inside macro invocations in the body. A macro expands to a token
        tree the fast engine doesn't parse as expressions, so a call written inside it (`format!("{}",
        f())`, `assert!(x.is_empty())`) produces no CALLS edge -- counted so the function can flag that
        its callees may be incomplete. A nested macro is counted once (via the outer's text)."""
        if body is None:
            return 0
        total, stack = 0, [body]
        while stack:
            n = stack.pop()
            if n.type == "macro_invocation":
                total += len(_MACRO_CALL_RE.findall(txt(n)))  # the macro name is `name!(` -> not matched
                continue  # its contents are tokens, not real call_expressions -- don't descend
            stack.extend(n.children)
        return total

    def build_function(node, kind: str, has_body: bool) -> RawFunction:
        params, self_kind = parameters(fieldn(node, "parameters"))
        ls, le = lines(node)
        return RawFunction(
            name=txt(fieldn(node, "name")),
            kind=kind,
            visibility=visibility(node),
            modifiers=modifiers(node),
            return_type=type_text(fieldn(node, "return_type")) or None,
            parameters=params,
            calls=collect_calls(fieldn(node, "body")),
            unresolved_macro_calls=count_macro_calls(fieldn(node, "body")),
            type_parameters=type_parameters(node),
            is_associated=(kind == "METHOD" and self_kind is None),
            self_kind=self_kind,
            has_body=has_body,
            source=txt(node),
            doc=doc_of(node),
            line_start=ls,
            line_end=le,
        )

    def struct_fields(body) -> list[RawField]:
        out: list[RawField] = []
        if body is None:
            return out
        if body.type == "field_declaration_list":
            pos = 0
            for c in body.named_children:
                if c.type == "field_declaration":
                    fls, fle = lines(c)
                    out.append(
                        RawField(
                            name=txt(fieldn(c, "name")),
                            type=type_text(fieldn(c, "type")),
                            visibility=visibility(c),
                            position=pos,
                            line_start=fls,
                            line_end=fle,
                        )
                    )
                    pos += 1
        elif body.type == "ordered_field_declaration_list":
            pos = 0
            pending_vis = "private"  # a `pub` sits as a sibling right before the field's type
            for i in range(body.child_count):
                ch = body.child(i)
                if ch.type == "visibility_modifier":
                    pending_vis = " ".join(txt(ch).split())
                elif body.field_name_for_child(i) == "type":
                    fls, fle = lines(ch)
                    out.append(
                        RawField(name=str(pos), type=type_text(ch), visibility=pending_vis, position=pos, line_start=fls, line_end=fle)
                    )
                    pos += 1
                    pending_vis = "private"
        return out

    def build_struct(node) -> RawType:
        ls, le = lines(node)
        return RawType(
            kind="Struct",
            name=txt(fieldn(node, "name")),
            visibility=visibility(node),
            type_parameters=type_parameters(node),
            fields=struct_fields(fieldn(node, "body")),
            source=txt(node),
            doc=doc_of(node),
            line_start=ls,
            line_end=le,
        )

    def build_enum(node) -> RawType:
        ls, le = lines(node)
        rt = RawType(
            kind="Enum",
            name=txt(fieldn(node, "name")),
            visibility=visibility(node),
            type_parameters=type_parameters(node),
            source=txt(node),
            doc=doc_of(node),
            line_start=ls,
            line_end=le,
        )
        body = fieldn(node, "body")
        if body is not None:
            for c in body.named_children:
                if c.type != "enum_variant":
                    continue
                vbody = fieldn(c, "body")
                if vbody is None:
                    kind = "unit"
                elif vbody.type == "ordered_field_declaration_list":
                    kind = "tuple"
                else:
                    kind = "struct"
                vls, vle = lines(c)
                rt.variants.append(
                    RawVariant(
                        name=txt(fieldn(c, "name")),
                        kind=kind,
                        fields=struct_fields(vbody),
                        line_start=vls,
                        line_end=vle,
                    )
                )
        return rt

    def build_trait(node) -> RawType:
        ls, le = lines(node)
        rt = RawType(
            kind="Trait",
            name=txt(fieldn(node, "name")),
            visibility=visibility(node),
            type_parameters=type_parameters(node),
            source=txt(node),
            doc=doc_of(node),
            line_start=ls,
            line_end=le,
        )
        bounds = fieldn(node, "bounds")
        if bounds is not None:
            for c in bounds.named_children:
                nm = simple_type_name(c)
                if nm:
                    rt.supertraits.append(nm)
        body = fieldn(node, "body")
        if body is not None:
            for c in body.named_children:
                if c.type == "function_item":
                    rt.trait_methods.append(build_function(c, "METHOD", has_body=True))
                elif c.type == "function_signature_item":
                    rt.trait_methods.append(build_function(c, "METHOD", has_body=False))
        return rt

    def build_impl(node) -> RawImpl:
        ls, le = lines(node)
        ri = RawImpl(
            type_name=simple_type_name(fieldn(node, "type")) or "",
            trait_name=simple_type_name(fieldn(node, "trait")),
            line_start=ls,
            line_end=le,
        )
        body = fieldn(node, "body")
        if body is not None:
            for c in body.named_children:
                if c.type == "function_item":
                    ri.methods.append(build_function(c, "METHOD", has_body=True))
                elif c.type == "function_signature_item":
                    ri.methods.append(build_function(c, "METHOD", has_body=False))
        return ri

    def build_const(node) -> RawConst:
        ls, le = lines(node)
        return RawConst(
            name=txt(fieldn(node, "name")),
            kind="const" if node.type == "const_item" else "static",
            type=type_text(fieldn(node, "type")),
            visibility=visibility(node),
            source=txt(node),
            line_start=ls,
            line_end=le,
        )

    def build_module(node) -> RawModule:
        ls, le = lines(node)
        rm = RawModule(
            name=txt(fieldn(node, "name")),
            visibility=visibility(node),
            source=txt(node),
            line_start=ls,
            line_end=le,
        )
        body = fieldn(node, "body")
        if body is None:
            rm.is_declaration = True  # `mod foo;` -- body lives in another file
            return rm
        collect_scope(body, rm)
        return rm

    def collect_scope(container_node, target) -> None:
        """Fill `target` (a RawFile or RawModule) from the items of `container_node`
        (a `source_file` or a `declaration_list`)."""
        for child in container_node.named_children:
            t = child.type
            if t in _TYPE_ITEMS:
                if t == "struct_item":
                    target.types.append(build_struct(child))
                elif t == "enum_item":
                    target.types.append(build_enum(child))
                else:
                    target.types.append(build_trait(child))
            elif t == "function_item":
                target.functions.append(build_function(child, "FUNCTION", has_body=True))
            elif t == "impl_item":
                target.impls.append(build_impl(child))
            elif t in ("const_item", "static_item"):
                target.consts.append(build_const(child))
            elif t == "mod_item":
                target.modules.append(build_module(child))
            elif t == "use_declaration" and hasattr(target, "uses"):
                target.uses.extend(_expand_use(fieldn(child, "argument")))

    def _expand_use(argument) -> list[str]:
        if argument is None:
            return []
        if argument.type == "scoped_use_list":  # `use a::b::{c, d};`
            prefix = txt(fieldn(argument, "path"))
            lst = fieldn(argument, "list")
            return [f"{prefix}::{txt(c)}" for c in lst.named_children] if lst is not None else []
        return [txt(argument)]

    rf = RawFile(path=path_label)
    collect_scope(tree.root_node, rf)
    return rf
