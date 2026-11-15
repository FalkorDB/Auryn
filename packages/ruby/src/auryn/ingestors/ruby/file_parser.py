"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared state
-- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned, no
tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-ruby 0.23.1
(see this package's grammar probe), not assumed:

- namespaces: `module` (`name` = constant|scope_resolution, `body` = body_statement) and `class`
  (same, plus optional `superclass` field whose named child is the base constant). `singleton_class`
  (`class << self`, `value`+`body`) contributes class-level methods to the enclosing type.
- callables: `method` (`name` = identifier|operator|setter, optional `parameters`
  = method_parameters, optional `body`) and `singleton_method` (`def self.x`/`def Obj.x`:
  `object`+`name`+`parameters`+`body`) -- the latter is a class/singleton method.
- `method_parameters` children: `identifier` (required), `optional_parameter` (`name`+`value`),
  `splat_parameter`/`hash_splat_parameter`/`block_parameter` (`name`), `keyword_parameter`
  (`name`, optional `value`).
- calls: `call` (`method` = identifier|super|operator|setter, optional `receiver`, optional
  `arguments` = argument_list, optional `block`). A receiverless no-arg no-paren call is a bare
  `identifier` in statement position (Ruby dynamism) -- captured too, guarded by the set of
  locals assigned in the same body. `super` is its own node when bare.
- class-body DSL calls: `include`/`prepend`/`extend Mod` -> mixins; `attr_accessor`/`attr_reader`/
  `attr_writer :x` -> fields; `private`/`protected`/`public` toggle the current visibility.
- `require`/`require_relative "path"` -> the require graph. Constants: an `assignment` whose
  `left` is a `constant` (uppercase-initial) -> RawConst.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.ruby.grammar import new_parser
from auryn.ingestors.ruby.raw_model import (
    RawCall,
    RawConst,
    RawField,
    RawFile,
    RawInclude,
    RawMethod,
    RawModule,
    RawParameter,
    RawRequire,
)

# Node types that introduce a fresh statement context: a bare identifier directly under one of
# these is a receiverless "command" call, not an argument/condition/operand (Ruby dynamism).
_STATEMENT_CONTAINERS = frozenset({"body_statement", "then", "else", "begin", "ensure"})
# Pseudo-keyword bare identifiers that are never method calls we want to record.
_BARE_SKIP = frozenset({
    "self", "nil", "true", "false", "__method__", "__FILE__", "__dir__",
    "private", "protected", "public", "super", "yield", "return", "break", "next", "redo", "retry",
})
_MIXIN_METHODS = {"include": "include", "prepend": "prepend", "extend": "extend"}
_ATTR_METHODS = {"attr_accessor": "accessor", "attr_reader": "reader", "attr_writer": "writer"}
_VISIBILITY_MARKERS = frozenset({"private", "protected", "public"})


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

    def doc_of(node) -> str:
        """The shared `doc` (leading `#`/`=begin` comment), cleaned via core.docstrings.

        tree-sitter-ruby hoists the leading comment of a block's *first* statement out of the
        `body_statement` and up onto the enclosing `class`/`module` node (so the first method's
        `prev_named_sibling` is None). When that happens, escape up to the body_statement so its
        preceding comment sibling is visible; otherwise the walk is the normal one."""
        anchor = node
        if anchor.prev_named_sibling is None and anchor.parent is not None and anchor.parent.type == "body_statement":
            anchor = anchor.parent
        return leading_comment(anchor, source)

    def const_name(node) -> str:
        """The written name of a `constant` or `scope_resolution` (`A::B`) node, collapsed."""
        return " ".join(txt(node).split()) if node is not None else ""

    def string_literal(node):
        """The text inside a `string` argument node, or None if it isn't a plain string."""
        if node is None or node.type != "string":
            return None
        parts = [txt(c) for c in node.named_children if c.type == "string_content"]
        return "".join(parts) if parts else txt(node).strip("\"'")

    # -- parameters -------------------------------------------------------------------------
    _PARAM_KINDS = {
        "splat_parameter": "splat",
        "hash_splat_parameter": "hash_splat",
        "block_parameter": "block",
    }

    def parameters(params_node) -> list[RawParameter]:
        if params_node is None:
            return []
        out: list[RawParameter] = []
        pos = 0
        for c in params_node.named_children:
            t = c.type
            if t == "identifier":
                out.append(RawParameter(name=txt(c), kind="required", position=pos))
            elif t == "optional_parameter":
                out.append(RawParameter(
                    name=txt(fieldn(c, "name")), kind="optional", position=pos,
                    default=txt(fieldn(c, "value")) or None,
                ))
            elif t == "keyword_parameter":
                val = fieldn(c, "value")
                out.append(RawParameter(
                    name=txt(fieldn(c, "name")),
                    kind="keyword_optional" if val is not None else "keyword",
                    position=pos, default=txt(val) if val is not None else None,
                ))
            elif t in _PARAM_KINDS:
                nm = fieldn(c, "name")
                out.append(RawParameter(name=txt(nm) if nm is not None else "", kind=_PARAM_KINDS[t], position=pos))
            else:
                continue
            pos += 1
        return out

    def signature(name: str, params: list[RawParameter]) -> str:
        pieces = []
        for p in params:
            if p.kind == "splat":
                pieces.append(f"*{p.name}")
            elif p.kind == "hash_splat":
                pieces.append(f"**{p.name}")
            elif p.kind == "block":
                pieces.append(f"&{p.name}")
            elif p.kind == "keyword":
                pieces.append(f"{p.name}:")
            elif p.kind == "keyword_optional":
                pieces.append(f"{p.name}: {p.default}")
            elif p.kind == "optional":
                pieces.append(f"{p.name} = {p.default}")
            else:
                pieces.append(p.name)
        return f"{name}({', '.join(pieces)})"

    # -- calls ------------------------------------------------------------------------------
    def method_name_text(node) -> str:
        """The method name of a `method`/`singleton_method`/`call` -- an identifier, an operator
        (`==`, `[]`), or a setter (`x=`, whose child holds the base name)."""
        if node is None:
            return ""
        if node.type == "setter":
            base = fieldn(node, "name")
            return f"{txt(base)}=" if base is not None else txt(node)
        return txt(node)

    def _local_names(body) -> set[str]:
        """Names bound as locals in `body`: assignment/operator_assignment targets, rescue
        variables, and block/for parameters -- so a bare-identifier command-call scan can skip a
        local read (which is syntactically identical to a paren-less call)."""
        locals_: set[str] = set()
        stack = [body]
        while stack:
            n = stack.pop()
            t = n.type
            if t in ("assignment", "operator_assignment"):
                left = fieldn(n, "left")
                if left is not None and left.type == "identifier":
                    locals_.add(txt(left))
            elif t == "exception_variable":
                for g in n.named_children:
                    if g.type == "identifier":
                        locals_.add(txt(g))
            elif t in ("block_parameters", "method_parameters"):
                for g in n.named_children:
                    nm = fieldn(g, "name") if g.type != "identifier" else g
                    if nm is not None and nm.type == "identifier":
                        locals_.add(txt(nm))
            stack.extend(n.children)
        return locals_

    def _receiver_call(node) -> RawCall | None:
        """A `call` node -> RawCall (or None to drop an unnameable/complex receiver)."""
        method = fieldn(node, "method")
        if method is not None and method.type == "super":
            return RawCall(name="super", kind="super")
        name = method_name_text(method)
        if not name:
            return None
        recv = fieldn(node, "receiver")
        if recv is None:
            return RawCall(name=name, kind="command")
        rt = recv.type
        if rt == "self":
            return RawCall(name=name, kind="self")
        if rt in ("constant", "scope_resolution"):
            return RawCall(name=name, kind="constant", qualifier=const_name(recv))
        if rt in ("identifier", "instance_variable", "class_variable", "global_variable"):
            return RawCall(name=name, kind="instance", receiver=txt(recv))
        return RawCall(name=name, kind="chain")  # a.b.c(), literal receivers, ... -> resolution drops

    def collect_calls(body) -> list[RawCall]:
        if body is None:
            return []
        locals_ = _local_names(body)
        out: list[RawCall] = []
        seen: set = set()

        def add(call: RawCall | None) -> None:
            if call is None or not call.name:
                return
            key = (call.name, call.kind, call.receiver, call.qualifier)
            if key not in seen:
                seen.add(key)
                out.append(call)

        stack = [(body, None)]
        while stack:
            n, parent_type = stack.pop()
            t = n.type
            if t == "call":
                add(_receiver_call(n))
            elif t == "super":  # a bare `super` (with args it parses as a `call`)
                add(RawCall(name="super", kind="super"))
            elif t == "identifier" and parent_type in _STATEMENT_CONTAINERS:
                name = txt(n)
                if name not in locals_ and name not in _BARE_SKIP:
                    add(RawCall(name=name, kind="command"))
            for c in n.children:
                stack.append((c, t))
        return out

    # -- methods ----------------------------------------------------------------------------
    def build_method(node, visibility: str, force_singleton: bool = False) -> RawMethod:
        is_singleton = force_singleton or node.type == "singleton_method"
        name = method_name_text(fieldn(node, "name"))
        params = parameters(fieldn(node, "parameters"))
        ls, le = lines(node)
        return RawMethod(
            name=name,
            kind="METHOD",  # rewritten to FUNCTION for a top-level def in collect_scope
            is_singleton=is_singleton,
            visibility=visibility,
            parameters=params,
            calls=collect_calls(fieldn(node, "body")),
            signature=signature(name, params),
            source=txt(node),
            doc=doc_of(node),
            line_start=ls,
            line_end=le,
        )

    # -- namespaces -------------------------------------------------------------------------
    def build_namespace(node, kind: str) -> RawModule:
        ls, le = lines(node)
        rm = RawModule(
            name=const_name(fieldn(node, "name")),
            kind=kind,
            source=txt(node),
            doc=doc_of(node),
            line_start=ls,
            line_end=le,
        )
        if kind == "Class":
            sc = fieldn(node, "superclass")
            if sc is not None:
                base = next((c for c in sc.named_children if c.type in ("constant", "scope_resolution")), None)
                rm.superclass = const_name(base) if base is not None else None
        collect_scope(fieldn(node, "body"), rm)
        return rm

    def _symbol_names(args_node) -> list[str]:
        """Symbol / string argument names from an `attr_*` argument list (`:name`, `"name"`)."""
        out: list[str] = []
        if args_node is None:
            return out
        for c in args_node.named_children:
            if c.type in ("simple_symbol", "symbol"):
                out.append(txt(c).lstrip(":").strip("\"'"))
            elif c.type == "string":
                s = string_literal(c)
                if s:
                    out.append(s)
        return out

    def handle_dsl_call(node, target) -> bool:
        """Interpret a class/module-body `call` that is a mixin/attr/require DSL. Returns True if
        it was consumed (so it isn't also treated as a plain statement call)."""
        method = fieldn(node, "method")
        if method is None or fieldn(node, "receiver") is not None:
            return False
        name = txt(method)
        args = fieldn(node, "arguments")
        if name in _MIXIN_METHODS and args is not None:
            # A file-scope `include`/`extend`/`prepend` (target is a RawFile) has no class/module to
            # attach to, so skip the append but still consume the call -- it's a mixin DSL, not a
            # regular method call to track (mirrors the `requires` guard below).
            if hasattr(target, "includes"):
                for a in args.named_children:
                    if a.type in ("constant", "scope_resolution"):
                        target.includes.append(RawInclude(name=const_name(a), kind=_MIXIN_METHODS[name]))
            return True
        if name in _ATTR_METHODS and args is not None:
            if hasattr(target, "fields"):
                ls, le = lines(node)
                for sym in _symbol_names(args):
                    target.fields.append(RawField(name=sym, access=_ATTR_METHODS[name], line_start=ls, line_end=le))
            return True
        if name in ("require", "require_relative") and args is not None and hasattr(target, "requires"):
            s = string_literal(args.named_children[0]) if args.named_children else None
            if s is not None:
                target.requires.append(RawRequire(target=s, kind=name))
            return True
        return False

    def collect_scope(container, target) -> None:
        """Fill `target` (a RawFile or RawModule) from the statements of `container` (a
        body_statement / the program root). Tracks the running method visibility inside a class."""
        if container is None:
            return
        visibility = "public"
        is_namespace = isinstance(target, RawModule)
        for child in container.named_children:
            t = child.type
            if t == "class":
                target.classes.append(build_namespace(child, "Class"))
            elif t == "module":
                target.modules.append(build_namespace(child, "Module"))
            elif t == "method":
                m = build_method(child, visibility)
                if not is_namespace:
                    m.kind = "FUNCTION"  # a top-level def is a free function
                target.methods.append(m)
            elif t == "singleton_method":
                target.methods.append(build_method(child, visibility))
            elif t == "singleton_class":  # `class << self` -> its defs are class methods
                body = fieldn(child, "body")
                if body is not None:
                    for m in body.named_children:
                        if m.type in ("method", "singleton_method"):
                            target.methods.append(build_method(m, "public", force_singleton=True))
            elif t == "assignment":
                left = fieldn(child, "left")
                if left is not None and left.type == "constant":
                    ls, le = lines(child)
                    target.consts.append(RawConst(
                        name=txt(left), value=" ".join(txt(fieldn(child, "right")).split()),
                        line_start=ls, line_end=le,
                    ))
            elif t == "identifier" and txt(child) in _VISIBILITY_MARKERS:
                visibility = txt(child)  # `private` / `protected` / `public` toggle
            elif t == "call":
                if handle_dsl_call(child, target):
                    continue
                # `private def foo; end` (a call wrapping a def) -> mark that method's visibility.
                m = fieldn(child, "method")
                args = fieldn(child, "arguments")
                if m is not None and txt(m) in _VISIBILITY_MARKERS and args is not None:
                    inner = next((a for a in args.named_children if a.type in ("method", "singleton_method")), None)
                    if inner is not None:
                        rm = build_method(inner, txt(m))
                        if not is_namespace:
                            rm.kind = "FUNCTION"
                        target.methods.append(rm)

    rf = RawFile(path=path_label)
    collect_scope(tree.root_node, rf)
    return rf
