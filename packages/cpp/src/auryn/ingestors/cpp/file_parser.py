"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned,
no tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic
and takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-cpp 0.23.4
(see the package's grammar probe), not assumed:

- top level / namespace scope: `namespace_definition` (`name` = `namespace_identifier` or
  `nested_namespace_specifier` for `a::b`; anonymous = no `name`; `body` = `declaration_list`),
  `preproc_include` (`path` = `system_lib_string` | `string_literal`), `preproc_def` /
  `preproc_function_def` (`name`), and the header-guard/`extern "C"` wrappers
  `preproc_ifdef`/`preproc_if`/.../`linkage_specification` (flattened, as in the C ingestor).
- types: `class_specifier` / `struct_specifier` / `union_specifier` (`name` = type_identifier,
  child `base_class_clause` [access_specifier + type], `body` = field_declaration_list) and
  `enum_specifier` (optional `class`/`struct` token = scoped; `name`; `body` = enumerator_list
  of `enumerator` with `name`).
- members (inside field_declaration_list, with `access_specifier` toggling current access):
  `field_declaration` (a member var, OR a method *declaration* when its declarator descends to
  a `function_declarator`), `function_definition` (an inline member method with a body), and
  `declaration` (a ctor/dtor prototype -- no return type -> parsed as declaration).
- functions: `function_definition` / `declaration` whose `function_declarator`'s inner
  declarator is a plain `identifier` = free function; a `qualified_identifier`
  (`scope`::`name`, possibly nested) = an out-of-line member definition `Owner::method`.
- templates: `template_declaration` (`parameters` = template_parameter_list of
  `type_parameter_declaration`) wrapping a class/function.
- calls: `call_expression` (`function` = `identifier` | `field_expression`
  [`argument`/`operator` `.`|`->` /`field`] | `qualified_identifier`) and `new_expression`
  (`type`).

Names sit at the bottom of possibly pointer/reference/array/function-wrapped declarators, so
the descent helpers step through the `declarator` field (and the unlabelled child of a
`reference_declarator`) to the leaf identifier/field_identifier/destructor_name/qualified id."""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.cpp.grammar import new_parser
from auryn.ingestors.cpp.raw_model import (
    RawBase,
    RawCall,
    RawField,
    RawFile,
    RawFunction,
    RawInclude,
    RawMacro,
    RawMethod,
    RawParameter,
    RawType,
)

_TYPE_SPECIFIERS = {"class_specifier": "Class", "struct_specifier": "Struct", "union_specifier": "Union"}
_NAME_LEAVES = {"identifier", "field_identifier", "type_identifier", "destructor_name"}
_DECL_WRAPPERS = {
    "pointer_declarator", "reference_declarator", "array_declarator",
    "init_declarator", "parenthesized_declarator",
}
# Wrappers that hold ordinary declarations as children rather than being declarations
# themselves -- header guards / feature #ifs and `extern "C"` blocks. We flatten them so a
# guarded header (`#ifndef FOO_HPP ... #endif`, i.e. almost every real header) isn't lost.
_TRANSPARENT = {
    "preproc_ifdef", "preproc_if", "preproc_elif", "preproc_else", "preproc_elifdef",
    "preproc_elifndef", "linkage_specification",
}


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str, parser=None) -> RawFile:
    # `parser` lets a sibling ingestor reuse this full C++ extraction with a different grammar.
    # tree-sitter-cuda is a strict superset of tree-sitter-cpp (identical node types/fields for
    # every C++ construct -- verified), so the auryn-cuda plugin passes its own parser here and
    # gets the whole C++ graph for free, then layers on the CUDA-only extras (see auryn.ingestors.cuda).
    tree = (parser or new_parser()).parse(source)
    rf = RawFile(path=path_label)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        # tree-sitter points are 0-based (row, col); expose 1-based inclusive line numbers.
        return node.start_point[0] + 1, node.end_point[0] + 1

    def doc_of(inner, outer=None) -> str:
        """The cleaned leading comment for a declaration. A doc comment is a *preceding sibling*
        of the node it documents; when a class/function is wrapped in a `template_declaration`,
        the comment sits above the wrapper (`outer`), not the inner specifier (`inner`)."""
        return leading_comment(inner, source) or (leading_comment(outer, source) if outer is not None else "")

    def type_text(type_node) -> str:
        return " ".join(txt(type_node).split()) if type_node else ""

    def first_type_name(node) -> str | None:
        """The bare simple name a type/base node denotes: `Base` for `Base`, `Base<T>`
        (template_type), `ns::Base` (qualified) -- generic args and scopes stripped."""
        if node is None:
            return None
        if node.type in ("type_identifier", "namespace_identifier"):
            return txt(node)
        if node.type == "template_type":
            return first_type_name(node.child_by_field_name("name"))
        if node.type == "qualified_identifier":
            segs, final = split_qualified(node)
            return final or (segs[-1] if segs else None)
        # descend to the first type_identifier we can find
        stack = list(node.children)
        while stack:
            n = stack.pop(0)
            if n.type == "type_identifier":
                return txt(n)
            stack.extend(n.children)
        return None

    def split_qualified(qid) -> tuple[list[str], str]:
        """`A::B::deep` -> (["A", "B"], "deep"); `Foo::bar` -> (["Foo"], "bar"). The scope
        segments are the leading names; the final segment is the simple name (which may be a
        destructor_name, kept verbatim as "~Foo")."""
        segs: list[str] = []
        node = qid
        while node is not None and node.type == "qualified_identifier":
            scope = node.child_by_field_name("scope")
            if scope is not None:
                segs.append(txt(scope))
            node = node.child_by_field_name("name")
        final = txt(node) if node is not None else ""
        return segs, final

    def name_node_of(decl):
        """Descend a (possibly pointer/reference/array/init/function-wrapped) declarator to the
        leaf name node: an identifier/field_identifier/destructor_name, or a qualified_identifier
        (an out-of-line `Owner::method`). None for an abstract (unnamed) declarator."""
        node = decl
        seen = 0
        while node is not None and seen < 64:
            if node.type in _NAME_LEAVES or node.type == "qualified_identifier":
                return node
            nxt = node.child_by_field_name("declarator")
            if nxt is None:  # e.g. reference_declarator: the inner node isn't a `declarator` field
                nxt = next((c for c in node.children
                            if c.type in _DECL_WRAPPERS or c.type == "function_declarator"), None)
            node = nxt
            seen += 1
        return None

    def find_function_declarator(decl):
        """A function/method name lives in a function_declarator, itself possibly wrapped by
        pointer/reference/array declarators (`const Point& ref() const`). Descend to it."""
        node = decl
        seen = 0
        while node is not None and seen < 64:
            if node.type == "function_declarator":
                return node
            nxt = node.child_by_field_name("declarator")
            if nxt is None:
                nxt = next((c for c in node.children
                            if c.type in _DECL_WRAPPERS or c.type == "function_declarator"), None)
            node = nxt
            seen += 1
        return None

    def written_type(container, name_node, upto=None) -> str:
        """The type exactly as written, minus the identifier being declared and any storage-class
        keyword: the container text with the name excised. Keeps `const`, `*`, `&`, `[]`, qualifiers that
        live in the declarator -- e.g. `const Shape *s` -> `const Shape *`. `upto` bounds the text to
        `[start, upto)` -- used for a defaulted parameter to cut at the `=` token, so the declaration and
        its out-of-line definition run the SAME normalization over the SAME type text and thus share an
        id (the default value never reaches the type), whatever the type contains."""
        a = container.start_byte
        b = upto if upto is not None else container.end_byte
        if name_node is not None and a <= name_node.start_byte and name_node.end_byte <= b:
            piece = source[a : name_node.start_byte] + source[name_node.end_byte : b]
        else:
            piece = source[a:b]
        text = " ".join(piece.decode("utf-8", "replace").split()).strip().rstrip(";").strip()
        for kw in ("static ", "mutable ", "inline ", "constexpr "):
            while text.startswith(kw):
                text = text[len(kw):].strip()
        if ":" in text and "::" not in text:  # bitfield `int x : 3` -- keep type, drop width
            text = text.split(":", 1)[0].strip()
        return text

    def signature_of(node, top_declarator) -> str:
        """The header exactly as written -- from the declaration start through the `)` of the
        parameter list, body excluded (top_declarator ends at the `)` for def and proto alike)."""
        if top_declarator is None:
            return ""
        return " ".join(source[node.start_byte : top_declarator.end_byte].decode("utf-8", "replace").split()).strip()

    def parameters(func_declarator) -> list[RawParameter]:
        plist = func_declarator.child_by_field_name("parameters")
        out: list[RawParameter] = []
        if plist is None:
            return out
        pos = 0
        for p in plist.children:
            # `optional_parameter_declaration` is a parameter WITH a default value (`T x = y`). It MUST be
            # treated as a parameter -- skipping it (as the old `!= "parameter_declaration"` did) drops the
            # param, so a declaration with a default gets a DIFFERENT signature/id than its out-of-line
            # definition (which has no default), the two never merge, and the decl-only node goes stale on
            # resync (incremental != full; also a duplicate-node bug in a plain full ingest). Seen on Catch2's
            # `Equals(std::string, CaseSensitive = CaseSensitive::Yes)`.
            if p.type not in ("parameter_declaration", "optional_parameter_declaration"):
                continue  # skips '(', ')', ',', and variadic '...'
            d = p.child_by_field_name("declarator")
            nnode = name_node_of(d) if d is not None else None
            pname = txt(nnode) if nnode is not None and nnode.type != "qualified_identifier" else ""
            if p.type == "optional_parameter_declaration":
                # A defaulted param (`T x = default`): bound written_type at the `=` TOKEN (a direct child --
                # the `==` inside `decltype(a==b)` is nested, not a child) so the default value and any comment
                # after `=` are dropped. Running it through the SAME written_type (same normalization) as a
                # plain param is what makes the decl and its out-of-line def type IDENTICALLY -- whatever the
                # type contains (ternary, bitfield-looking `:`, etc.) -- so they share an id and merge (else
                # the decl-only node is a duplicate that goes stale on resync; Catch2 finding).
                eq = next((c for c in p.children if not c.is_named and txt(c) == "="), None)
                t_written = written_type(p, nnode, upto=eq.start_byte if eq is not None else None)
            else:
                t_written = written_type(p, nnode)
            if t_written == "void" and d is None:
                continue  # `(void)` is "no parameters"
            out.append(RawParameter(name=pname, type=t_written, position=pos))
            pos += 1
        return out

    def has_child_token(node, token: str) -> bool:
        return any(not c.is_named and txt(c) == token for c in node.children) or \
            any(c.type == token for c in node.children)

    def is_static(node) -> bool:
        return any(c.type == "storage_class_specifier" and txt(c) == "static" for c in node.children)

    def is_virtual(node) -> bool:
        return any((not c.is_named and txt(c) == "virtual") for c in node.children)

    def is_const_method(func_declarator) -> bool:
        return any(c.type == "type_qualifier" and txt(c) == "const" for c in func_declarator.children)

    def collect_calls(body) -> list[RawCall]:
        if body is None:
            return []
        out: list[RawCall] = []
        seen: set = set()
        stack = list(body.children)
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                call = None
                if fn is not None and fn.type == "identifier":
                    call = RawCall(name=txt(fn))
                elif fn is not None and fn.type == "field_expression":
                    arg = fn.child_by_field_name("argument")
                    fld = fn.child_by_field_name("field")
                    recv = txt(arg) if arg is not None and arg.type in ("identifier", "this") else None
                    if fld is not None:
                        call = RawCall(name=txt(fld), receiver=recv)
                elif fn is not None and fn.type == "qualified_identifier":
                    segs, final = split_qualified(fn)
                    if final:
                        call = RawCall(name=final, scope=segs[-1] if segs else None)
                if call is not None and call.name:
                    key = (call.name, call.receiver, call.scope, call.is_new)
                    if key not in seen:
                        seen.add(key)
                        out.append(call)
            elif n.type == "new_expression":
                nm = first_type_name(n.child_by_field_name("type"))
                if nm:
                    key = (nm, None, None, True)
                    if key not in seen:
                        seen.add(key)
                        out.append(RawCall(name=nm, is_new=True))
            stack.extend(n.children)
        return out

    # -- members -------------------------------------------------------------------------

    def method_from_declarator(node, fdecl, class_name: str, kind_hint: str | None,
                               is_def: bool) -> RawMethod | None:
        name_node = fdecl.child_by_field_name("declarator")
        leaf = name_node_of(name_node) if name_node is not None else None
        if leaf is None:
            return None
        if leaf.type == "destructor_name":
            name, kind = txt(leaf), "DESTRUCTOR"
        elif leaf.type == "identifier" and txt(leaf) == class_name:
            name, kind = txt(leaf), "CONSTRUCTOR"
        else:
            name, kind = txt(leaf), (kind_hint or "ORDINARY")
        ret = None if kind in ("CONSTRUCTOR", "DESTRUCTOR") else type_text(node.child_by_field_name("type"))
        body = node.child_by_field_name("body")
        ls, le = lines(node)
        return RawMethod(
            name=name, kind=kind, return_type=ret, params=parameters(fdecl),
            is_static=is_static(node), is_virtual=is_virtual(node), is_const=is_const_method(fdecl),
            is_definition=is_def, calls=collect_calls(body) if body is not None else [],
            source=txt(node), doc=doc_of(node), line_start=ls, line_end=le,
        )

    def build_members(field_list, rt: RawType) -> None:
        for m in field_list.children:
            if m.type == "access_specifier":
                continue  # (access tracking is not currently exposed; kept simple)
            if m.type in _TYPE_SPECIFIERS or m.type == "enum_specifier":
                nested = build_type(m, rt.namespace)
                if nested is not None:
                    rt.nested_types.append(nested)
            elif m.type == "template_declaration":
                inner, tparams = _unwrap_template(m)
                if inner is not None and (inner.type in _TYPE_SPECIFIERS or inner.type == "enum_specifier"):
                    nested = build_type(inner, rt.namespace, outer=m)
                    if nested is not None:
                        nested.type_parameters = tparams
                        rt.nested_types.append(nested)
            elif m.type == "function_definition":
                fdecl = find_function_declarator(m.child_by_field_name("declarator"))
                if fdecl is not None:
                    meth = method_from_declarator(m, fdecl, rt.name, None, is_def=True)
                    if meth is not None:
                        rt.methods.append(meth)
            elif m.type == "field_declaration":
                decl = m.child_by_field_name("declarator")
                fdecl = find_function_declarator(decl) if decl is not None else None
                if fdecl is not None:  # a method declaration (`int f() const;`, `static int g();`)
                    meth = method_from_declarator(m, fdecl, rt.name, "ORDINARY", is_def=False)
                    if meth is not None:
                        rt.methods.append(meth)
                else:  # a member variable
                    nnode = name_node_of(decl) if decl is not None else None
                    fname = txt(nnode) if nnode is not None and nnode.type in _NAME_LEAVES else ""
                    if fname:
                        ls, le = lines(m)
                        rt.fields.append(RawField(name=fname, type=written_type(m, nnode),
                                                  is_static=is_static(m), line_start=ls, line_end=le))
            elif m.type == "declaration":  # ctor/dtor prototype (no return type)
                decl = m.child_by_field_name("declarator")
                fdecl = find_function_declarator(decl) if decl is not None else None
                if fdecl is not None:
                    meth = method_from_declarator(m, fdecl, rt.name, "ORDINARY", is_def=False)
                    if meth is not None:
                        rt.methods.append(meth)

    def build_type(spec, namespace: str, outer=None) -> RawType | None:
        if spec.type == "enum_specifier":
            return build_enum(spec, namespace, outer)
        kind = _TYPE_SPECIFIERS[spec.type]
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            return None  # anonymous struct/union/class -- no stable identity, skip
        body = spec.child_by_field_name("body")
        ls, le = lines(spec)
        rt = RawType(kind=kind, name=txt(name_node), namespace=namespace,
                     is_definition=body is not None, source=txt(spec), doc=doc_of(spec, outer),
                     line_start=ls, line_end=le)
        rt.bases = base_classes(spec, default_access="public" if kind in ("Struct", "Union") else "private")
        if body is not None:
            build_members(body, rt)
        return rt

    def base_classes(spec, default_access: str) -> list[RawBase]:
        clause = next((c for c in spec.children if c.type == "base_class_clause"), None)
        if clause is None:
            return []
        out: list[RawBase] = []
        access = default_access
        for c in clause.children:
            if c.type == "access_specifier":
                access = txt(c)
            elif c.type in ("type_identifier", "template_type", "qualified_identifier"):
                nm = first_type_name(c)
                if nm:
                    out.append(RawBase(name=nm, access=access))
                access = default_access  # reset for the next base
        return out

    def build_enum(spec, namespace: str, outer=None) -> RawType:
        name_node = spec.child_by_field_name("name")
        body = spec.child_by_field_name("body")
        ls, le = lines(spec)
        rt = RawType(kind="Enum", name=txt(name_node) if name_node is not None else "",
                     namespace=namespace, is_scoped=has_child_token(spec, "class") or has_child_token(spec, "struct"),
                     is_definition=body is not None, source=txt(spec), doc=doc_of(spec, outer),
                     line_start=ls, line_end=le)
        if body is not None:
            for e in body.children:
                if e.type == "enumerator":
                    rt.enum_members.append(txt(e.child_by_field_name("name")))
        return rt

    # -- free functions / out-of-line member defs ----------------------------------------

    def build_function_or_memberdef(node, namespace: str, is_def: bool, outer=None) -> None:
        decl = node.child_by_field_name("declarator")
        fdecl = find_function_declarator(decl) if decl is not None else None
        if fdecl is None:
            return  # a variable declaration at namespace scope -- not modelled (no globals tier)
        name_node = fdecl.child_by_field_name("declarator")
        leaf = name_node_of(name_node) if name_node is not None else None
        if leaf is None:
            return
        body = node.child_by_field_name("body")
        ls, le = lines(node)
        if leaf.type == "qualified_identifier":
            segs, final = split_qualified(leaf)
            owner = segs[-1] if segs else None
            if final.startswith("~"):
                kind = "DESTRUCTOR"
            elif owner is not None and final == owner:
                kind = "CONSTRUCTOR"
            else:
                kind = "ORDINARY"
            ret = None if kind in ("CONSTRUCTOR", "DESTRUCTOR") else type_text(node.child_by_field_name("type"))
            rf.member_defs.append(RawMethod(
                name=final, kind=kind, return_type=ret, params=parameters(fdecl),
                is_static=is_static(node), is_const=is_const_method(fdecl),
                is_definition=body is not None, calls=collect_calls(body) if body is not None else [],
                owner_name=owner, owner_scope=segs, namespace=namespace,
                source=txt(node), doc=doc_of(node, outer), line_start=ls, line_end=le,
            ))
        else:  # plain identifier -> a free function
            name = txt(leaf)
            if not name:
                return
            rf.functions.append(RawFunction(
                name=name, namespace=namespace,
                return_type=type_text(node.child_by_field_name("type")),
                params=parameters(fdecl), is_definition=is_def,
                calls=collect_calls(body) if body is not None else [],
                signature=signature_of(node, decl), source=txt(node), doc=doc_of(node, outer),
                line_start=ls, line_end=le,
            ))

    # -- template unwrapping -------------------------------------------------------------

    def _unwrap_template(node):
        """(inner declaration node, template type-parameter names). Handles a single template
        prefix; nested `template<>` layers are unwrapped recursively."""
        tparams: list[str] = []
        tpl = node.child_by_field_name("parameters")
        if tpl is not None:
            for c in tpl.named_children:
                if c.type in ("type_parameter_declaration", "template_template_parameter_declaration"):
                    ident = next((g for g in c.children if g.type == "type_identifier"), None)
                    if ident is not None:
                        tparams.append(txt(ident))
        inner = next((c for c in node.children
                      if c.type in _TYPE_SPECIFIERS or c.type == "enum_specifier"
                      or c.type in ("function_definition", "declaration", "template_declaration")), None)
        if inner is not None and inner.type == "template_declaration":
            inner, more = _unwrap_template(inner)
            tparams.extend(more)
        return inner, tparams

    # -- top-level / namespace walk ------------------------------------------------------

    def walk(node, ns_path: list[str]) -> None:
        for child in node.children:
            kind = child.type
            if kind == "namespace_definition":
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name_node is None:  # anonymous namespace -- attribute contents to the parent
                    new_ns = ns_path
                elif name_node.type == "nested_namespace_specifier":
                    new_ns = ns_path + [txt(g) for g in name_node.children if g.type == "namespace_identifier"]
                else:
                    new_ns = ns_path + [txt(name_node)]
                if body is not None:
                    walk(body, new_ns)
            elif kind in _TRANSPARENT or kind == "declaration_list":
                walk(child, ns_path)
            elif kind == "preproc_include":
                p = child.child_by_field_name("path")
                if p is not None:
                    rf.includes.append(RawInclude(path=txt(p).strip('<>"'),
                                                  is_system=p.type == "system_lib_string"))
            elif kind == "preproc_def":
                ls, le = lines(child)
                rf.macros.append(RawMacro(name=txt(child.child_by_field_name("name")),
                                          is_function_like=False, source=txt(child).rstrip(),
                                          line_start=ls, line_end=le))
            elif kind == "preproc_function_def":
                ls, le = lines(child)
                rf.macros.append(RawMacro(name=txt(child.child_by_field_name("name")),
                                          is_function_like=True, source=txt(child).rstrip(),
                                          line_start=ls, line_end=le))
            elif kind in _TYPE_SPECIFIERS or kind == "enum_specifier":
                rt = build_type(child, "::".join(ns_path))
                if rt is not None:
                    rf.types.append(rt)
            elif kind == "template_declaration":
                inner, tparams = _unwrap_template(child)
                if inner is None:
                    continue
                if inner.type in _TYPE_SPECIFIERS or inner.type == "enum_specifier":
                    rt = build_type(inner, "::".join(ns_path), outer=child)
                    if rt is not None:
                        rt.type_parameters = tparams
                        rf.types.append(rt)
                elif inner.type in ("function_definition", "declaration"):
                    build_function_or_memberdef(inner, "::".join(ns_path), is_def=inner.type == "function_definition", outer=child)
            elif kind == "function_definition":
                build_function_or_memberdef(child, "::".join(ns_path), is_def=True)
            elif kind == "declaration":
                build_function_or_memberdef(child, "::".join(ns_path), is_def=False)

    walk(tree.root_node, [])
    return rf
