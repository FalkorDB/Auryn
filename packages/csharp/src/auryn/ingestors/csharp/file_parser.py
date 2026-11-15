"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (see the C# ingestor
plan's "Extraction pipeline"). Pure function, no shared state -- safe to run under a
ProcessPoolExecutor. Only picklable raw_model dataclasses are returned; no
tree-sitter node crosses that boundary.

parse_source(source, path_label) holds the actual logic and takes raw bytes
directly, so content sourced from somewhere other than the local filesystem (e.g.
a git blob, in ingestors/csharp/git_sync.py) can be parsed without ever being
written to a temp file first; parse_file is a thin, disk-reading wrapper around it.

Every field name/shape referenced below (e.g. method_declaration's "returns" field,
record_declaration's positional -- not field-named -- parameter_list, the params-
parameter grammar quirk in _parameters) was confirmed empirically against
tree-sitter-c-sharp 0.23.5 before writing this, not assumed from general C# grammar
knowledge -- see grammar.py's docstring for why this parser was chosen.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.core.http_seam import looks_like_route
from auryn.ingestors.csharp.grammar import new_parser
from auryn.ingestors.csharp.raw_model import (
    RawAttribute,
    RawCall,
    RawEnumMember,
    RawEvent,
    RawField,
    RawFile,
    RawMethod,
    RawParameter,
    RawProperty,
    RawType,
)

_TYPE_DECL_KINDS = {
    "class_declaration": "Class",
    "interface_declaration": "Interface",
    "struct_declaration": "Struct",
    "enum_declaration": "Enum",
    "record_declaration": "Record",
    "delegate_declaration": "Delegate",
}


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    tree = new_parser().parse(source)
    raw_file = RawFile(path=path_label)

    # A file-scoped namespace (`namespace Foo.Bar;`, C# 10+) doesn't wrap the rest of
    # the file as AST children the way a block-scoped one does -- every declaration
    # after it remains a flat sibling at the compilation_unit level, so its namespace
    # has to be tracked as loop state instead of via recursion.
    current_namespace = ""
    for child in tree.root_node.children:
        if child.type == "using_directive":
            ns = _using_namespace(child)
            if ns:
                raw_file.using_namespaces.append(ns)
        elif child.type == "file_scoped_namespace_declaration":
            name_node = child.child_by_field_name("name")
            current_namespace = name_node.text.decode() if name_node is not None else ""
        elif child.type == "namespace_declaration":
            _walk_namespace(child, "", raw_file, source)
        elif child.type in _TYPE_DECL_KINDS:
            raw_file.types.append(_build_type(child, current_namespace, source))

    return raw_file


def _walk_namespace(node, parent_namespace: str, raw_file: RawFile, source: bytes) -> None:
    name_node = node.child_by_field_name("name")
    # A block-scoped namespace's name field is a single node whose own text is
    # already the full dotted segment (e.g. "Foo.Bar" as one qualified_name) --
    # nesting isn't expressed by chained namespace_declarations for a dotted name,
    # only by an actual nested `namespace X { ... }` block inside this one's body.
    segment = name_node.text.decode() if name_node is not None else ""
    full_namespace = f"{parent_namespace}.{segment}" if parent_namespace else segment

    body = node.child_by_field_name("body")
    if body is None:
        return
    for child in body.children:
        if child.type == "namespace_declaration":
            _walk_namespace(child, full_namespace, raw_file, source)
        elif child.type in _TYPE_DECL_KINDS:
            raw_file.types.append(_build_type(child, full_namespace, source))


def _using_namespace(node) -> str | None:
    # No field name here (confirmed empirically) -- and for an alias form
    # (`using Foo = System.Collections.Generic;`) there are two named children (the
    # alias, then the real target), so the target is always the *last* one, not the
    # first.
    named = node.named_children
    return named[-1].text.decode() if named else None


def _build_type(node, namespace: str, source: bytes) -> RawType:
    kind = _TYPE_DECL_KINDS[node.type]
    name_node = node.child_by_field_name("name")
    modifiers = _modifiers(node)

    raw_type = RawType(
        kind=kind,
        name=name_node.text.decode() if name_node is not None else "",
        namespace=namespace,
        accessibility=_accessibility(modifiers),
        modifiers=modifiers,
        type_parameters=_type_parameters(node),
        attributes=_attributes(node),
        source=_type_text(node),
        doc=leading_comment(node, source),
    )

    if kind == "Record":
        # "record struct"/"readonly record struct" has an extra positional `struct`
        # token; plain/"record class" doesn't (or has an explicit `class` token).
        raw_type.is_value_type = any(c.type == "struct" for c in node.children)

    if kind == "Enum":
        raw_type.underlying_type = _enum_underlying_type(node)
    else:
        raw_type.base_list = _base_list(node)

    if kind == "Delegate":
        raw_type.return_type = _type_text(node.child_by_field_name("type"))
        raw_type.positional_parameters = _parameters(node.child_by_field_name("parameters"))
        return raw_type

    if kind == "Record":
        # Unlike method_declaration, record_declaration's primary-constructor
        # parameter_list has no field name -- it's positional (confirmed empirically).
        param_list = next((c for c in node.children if c.type == "parameter_list"), None)
        raw_type.positional_parameters = _parameters(param_list)

    body = node.child_by_field_name("body")
    if body is None:
        return raw_type

    if body.type == "enum_member_declaration_list":
        raw_type.enum_members = _enum_members(body)
        return raw_type

    for member in body.children:
        _dispatch_member(member, raw_type, source)

    return raw_type


def _dispatch_member(node, raw_type: RawType, source: bytes) -> None:
    if node.type == "field_declaration":
        raw_type.fields.extend(_build_fields(node))
    elif node.type == "event_field_declaration":
        raw_type.events.extend(_build_events(node))
    elif node.type == "property_declaration":
        raw_type.properties.append(_build_property(node))
    elif node.type == "method_declaration":
        raw_type.methods.append(_build_method(node, "ORDINARY", source))
    elif node.type == "constructor_declaration":
        raw_type.methods.append(_build_method(node, "CONSTRUCTOR", source))
    elif node.type == "destructor_declaration":
        raw_type.methods.append(_build_method(node, "DESTRUCTOR", source))
    elif node.type == "operator_declaration":
        raw_type.methods.append(_build_operator_method(node, source))
    elif node.type == "conversion_operator_declaration":
        raw_type.methods.append(_build_conversion_operator_method(node, source))
    elif node.type in _TYPE_DECL_KINDS:
        raw_type.nested_types.append(_build_type(node, raw_type.namespace, source))
    # Anything else (indexer_declaration, custom-accessor event_declaration, static
    # constructors, etc.) is silently skipped -- best-effort, same philosophy as the
    # PL/SQL ingestor's unrecognized ALL_DEPENDENCIES object types.


def _build_fields(node) -> list[RawField]:
    modifiers = _modifiers(node)
    accessibility = _accessibility(modifiers)
    attrs = _attributes(node)
    is_const = "const" in modifiers
    is_readonly = "readonly" in modifiers
    is_static = "static" in modifiers or is_const

    var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
    if var_decl is None:
        return []
    type_text = _type_text(var_decl.child_by_field_name("type"))

    fields = []
    for declarator in var_decl.children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        fields.append(
            RawField(
                name=name_node.text.decode() if name_node is not None else declarator.text.decode(),
                accessibility=accessibility,
                type=type_text,
                is_readonly=is_readonly,
                is_const=is_const,
                is_static=is_static,
                attributes=attrs,
            )
        )
    return fields


def _build_events(node) -> list[RawEvent]:
    modifiers = _modifiers(node)
    accessibility = _accessibility(modifiers)
    attrs = _attributes(node)
    is_static = "static" in modifiers

    var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
    if var_decl is None:
        return []
    type_text = _type_text(var_decl.child_by_field_name("type"))

    events = []
    for declarator in var_decl.children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        events.append(
            RawEvent(
                name=name_node.text.decode() if name_node is not None else declarator.text.decode(),
                accessibility=accessibility,
                type=type_text,
                is_static=is_static,
                attributes=attrs,
            )
        )
    return events


def _build_property(node) -> RawProperty:
    modifiers = _modifiers(node)
    accessibility = _accessibility(modifiers)
    is_static = "static" in modifiers
    attrs = _attributes(node)
    name_node = node.child_by_field_name("name")
    type_text = _type_text(node.child_by_field_name("type"))

    accessors_node = node.child_by_field_name("accessors")
    value_node = node.child_by_field_name("value")
    if accessors_node is not None:
        has_getter = False
        has_setter = False
        is_auto = True
        for accessor in accessors_node.named_children:
            if accessor.type != "accessor_declaration":
                continue
            accessor_name_node = accessor.child_by_field_name("name")
            keyword = accessor_name_node.type if accessor_name_node is not None else ""
            if keyword == "get":
                has_getter = True
            elif keyword in ("set", "init"):
                has_setter = True
            if accessor.child_by_field_name("body") is not None:
                is_auto = False
    elif value_node is not None:
        # `=> expr;` form -- always getter-only, and never auto-implemented (it has
        # a computed body, just spelled with an arrow instead of accessor braces).
        has_getter, has_setter, is_auto = True, False, False
    else:
        has_getter, has_setter, is_auto = False, False, False

    return RawProperty(
        name=name_node.text.decode() if name_node is not None else "",
        accessibility=accessibility,
        type=type_text,
        has_getter=has_getter,
        has_setter=has_setter,
        is_auto_implemented=is_auto,
        is_static=is_static,
        attributes=attrs,
    )


def _build_method(node, kind: str, source: bytes) -> RawMethod:
    modifiers = _modifiers(node)
    accessibility = _accessibility(modifiers)
    attrs = _attributes(node)
    name_node = node.child_by_field_name("name")
    name = name_node.text.decode() if name_node is not None else kind.lower()
    return_type = _type_text(node.child_by_field_name("returns")) if kind == "ORDINARY" else None
    params = _parameters(node.child_by_field_name("parameters"))
    body_node = node.child_by_field_name("body")

    return RawMethod(
        name=name,
        kind=kind,
        accessibility=accessibility,
        modifiers=modifiers,
        return_type=return_type,
        parameters=params,
        attributes=attrs,
        source=_type_text(node),
        doc=leading_comment(node, source),
        calls=_extract_calls(body_node) if body_node is not None else [],
    )


def _build_operator_method(node, source: bytes) -> RawMethod:
    modifiers = _modifiers(node)
    accessibility = _accessibility(modifiers)
    attrs = _attributes(node)
    return_type = _type_text(node.child_by_field_name("type"))
    op_node = node.child_by_field_name("operator")
    op_text = op_node.text.decode() if op_node is not None else "?"
    params = _parameters(node.child_by_field_name("parameters"))
    body_node = node.child_by_field_name("body")

    return RawMethod(
        name=f"operator{op_text}",
        kind="OPERATOR",
        accessibility=accessibility,
        modifiers=modifiers,
        return_type=return_type,
        parameters=params,
        attributes=attrs,
        source=_type_text(node),
        doc=leading_comment(node, source),
        calls=_extract_calls(body_node) if body_node is not None else [],
    )


def _build_conversion_operator_method(node, source: bytes) -> RawMethod:
    modifiers = _modifiers(node)
    accessibility = _accessibility(modifiers)
    attrs = _attributes(node)
    return_type = _type_text(node.child_by_field_name("type"))
    conversion_kind = next((c.text.decode() for c in node.children if c.type in ("implicit", "explicit")), "explicit")
    params = _parameters(node.child_by_field_name("parameters"))
    body_node = node.child_by_field_name("body")

    return RawMethod(
        name=f"operator {conversion_kind} {return_type}",
        kind="OPERATOR",
        accessibility=accessibility,
        modifiers=modifiers,
        return_type=return_type,
        parameters=params,
        attributes=attrs,
        source=_type_text(node),
        doc=leading_comment(node, source),
        calls=_extract_calls(body_node) if body_node is not None else [],
    )


def _parameters(parameter_list_node) -> list[RawParameter]:
    if parameter_list_node is None:
        return []

    parameters: list[RawParameter] = []
    position = 0
    pending_params = False
    pending_type = None

    for i, child in enumerate(parameter_list_node.children):
        field = parameter_list_node.field_name_for_child(i)
        if child.type == "params":
            pending_params = True
        elif child.type == "parameter":
            parameters.append(_build_parameter(child, position, pending_params))
            position += 1
            pending_params = False
        elif field == "type":
            # The `params T[] name` grammar quirk: type/name are attached directly to
            # parameter_list instead of wrapped in a `parameter` node (confirmed
            # empirically -- see module docstring).
            pending_type = child
        elif field == "name" and pending_type is not None:
            parameters.append(
                RawParameter(
                    name=child.text.decode(),
                    type=_type_text(pending_type),
                    direction="PARAMS" if pending_params else "VALUE",
                    position=position,
                    has_default=False,
                )
            )
            position += 1
            pending_params = False
            pending_type = None

    return parameters


def _build_parameter(node, position: int, forced_params: bool) -> RawParameter:
    type_node = node.child_by_field_name("type")
    name_node = node.child_by_field_name("name")
    direction = "PARAMS" if forced_params else "VALUE"
    for child in node.children:
        if child.type == "modifier":
            text = child.text.decode()
            if text in ("ref", "out", "in"):
                direction = text.upper()
    has_default = any(child.type == "=" for child in node.children)

    return RawParameter(
        name=name_node.text.decode() if name_node is not None else "",
        type=_type_text(type_node),
        direction=direction,
        position=position,
        has_default=has_default,
        attributes=_attributes(node),
    )


def _enum_underlying_type(node) -> str | None:
    base_list_node = next((c for c in node.children if c.type == "base_list"), None)
    if base_list_node is None:
        return None
    named = base_list_node.named_children
    return named[0].text.decode() if named else None


def _enum_members(body) -> list[RawEnumMember]:
    members = []
    for child in body.named_children:
        if child.type != "enum_member_declaration":
            continue
        name_node = child.child_by_field_name("name")
        value_node = child.child_by_field_name("value")
        members.append(
            RawEnumMember(
                name=name_node.text.decode() if name_node is not None else "",
                value=value_node.text.decode() if value_node is not None else None,
            )
        )
    return members


def _base_list(node) -> list[str]:
    base_list_node = next((c for c in node.children if c.type == "base_list"), None)
    if base_list_node is None:
        return []
    return [_base_entry_name(c) for c in base_list_node.named_children]


def _base_entry_name(node) -> str:
    if node.type == "generic_name":
        inner = node.children[0]
        return _base_entry_name(inner) if inner.type == "qualified_name" else inner.text.decode()
    if node.type == "qualified_name":
        name_node = node.child_by_field_name("name")
        return name_node.text.decode() if name_node is not None else node.text.decode()
    return node.text.decode()


def _modifiers(node) -> list[str]:
    return [c.text.decode() for c in node.children if c.type == "modifier"]


def _accessibility(modifiers: list[str]) -> str:
    has = lambda kw: kw in modifiers  # noqa: E731
    if has("private") and has("protected"):
        return "private protected"
    if has("protected") and has("internal"):
        return "protected internal"
    for kw in ("public", "private", "protected", "internal"):
        if has(kw):
            return kw
    # C#'s real default varies by context (private for class members, public for
    # interface members, internal for top-level types) -- collapsing to one default
    # is a deliberate simplification; accessibility here is informational only, never
    # used to decide what to ingest.
    return "internal"


def _type_parameters(node) -> list[str]:
    tp_list = node.child_by_field_name("type_parameters")
    if tp_list is None:
        # class/struct/record don't field-name this (unlike interface_declaration) --
        # confirmed empirically to be a genuine grammar inconsistency, not a mistake.
        tp_list = next((c for c in node.children if c.type == "type_parameter_list"), None)
    if tp_list is None:
        return []
    return [
        c.child_by_field_name("name").text.decode() for c in tp_list.named_children if c.type == "type_parameter"
    ]


def _attributes(node) -> list[RawAttribute]:
    attrs = []
    for child in node.children:
        if child.type != "attribute_list":
            continue
        for attr_node in child.named_children:
            if attr_node.type == "attribute":
                attrs.append(_build_attribute(attr_node))
    return attrs


def _build_attribute(node) -> RawAttribute:
    name_node = node.child_by_field_name("name")
    name = name_node.text.decode() if name_node is not None else node.text.decode()
    # Normalize the rarely-used explicit "Attribute" suffix (`[HttpGetAttribute(...)]`
    # means the same thing as `[HttpGet(...)]`) so every consumer can match on one
    # spelling.
    if name.endswith("Attribute") and name != "Attribute":
        name = name[: -len("Attribute")]
    args = []
    arg_list = next((c for c in node.children if c.type == "attribute_argument_list"), None)
    if arg_list is not None:
        for arg_node in arg_list.named_children:
            if arg_node.type == "attribute_argument":
                args.append(_attribute_argument_text(arg_node))
    return RawAttribute(name=name, arguments=args)


def _attribute_argument_text(node) -> str:
    # Best-effort: unwrap a bare string literal (covers the common
    # Route("template")/HttpGet("template") case, named or positional) without a full
    # C# constant-expression evaluator; anything else falls back to raw source text.
    string_literal = next((c for c in node.named_children if c.type == "string_literal"), None)
    if string_literal is not None:
        content = next((c for c in string_literal.named_children if c.type == "string_literal_content"), None)
        return content.text.decode() if content is not None else string_literal.text.decode()
    return node.text.decode()


def _extract_calls(node) -> list[RawCall]:
    calls: list[RawCall] = []
    _walk_calls(node, calls)
    return calls


# HttpClient call idioms (ADR 0006). Tree-sitter has no types, so detection is by the distinctive
# async method name + a URL-shaped first string arg (looks_like_route) -- so `repo.GetAsync("key")`
# is not mistaken for HTTP. The Roslyn deep pass detects the same calls precisely (by type).
_CS_HTTP_METHODS = {
    "getasync": "GET", "getstringasync": "GET", "getbytearrayasync": "GET", "getstreamasync": "GET",
    "getfromjsonasync": "GET", "postasync": "POST", "postasjsonasync": "POST",
    "putasync": "PUT", "putasjsonasync": "PUT", "deleteasync": "DELETE", "deletefromjsonasync": "DELETE",
    "patchasync": "PATCH", "patchasjsonasync": "PATCH",
}


def _walk_calls(node, calls: list[RawCall]) -> None:
    if node.type == "invocation_expression":
        func = node.child_by_field_name("function")
        if func is not None:
            call = _call_reference(func)
            verb = _CS_HTTP_METHODS.get(call.name.lower())
            if verb is not None:  # gate the arg walk on the cheap name check (perf)
                url = _first_string_arg(node)
                if url is not None and looks_like_route(url):
                    call.http_url, call.http_method = url, verb
            calls.append(call)
    for child in node.children:
        _walk_calls(child, calls)


def _first_string_arg(invocation) -> str | None:
    """The first string argument of an invocation, unquoted -- the request URL for an HttpClient
    call. Interpolated URLs (`$"api/x/{id}"`) become `api/x/{param}` so they match a route template."""
    args = invocation.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.children:
        for node in (arg, *arg.children):
            if node.type in ("string_literal", "verbatim_string_literal", "raw_string_literal"):
                text = node.text.decode()
                if text.startswith("@"):
                    text = text[1:]
                return text.strip('"')
    for arg in args.children:
        for node in (arg, *arg.children):
            if node.type == "interpolated_string_expression":
                return _interpolated_route(node.text.decode())
    return None


def _interpolated_route(text: str) -> str:
    """`$"api/widgets/{id}"` -> `api/widgets/{param}`: replace each top-level `{...}` (a C#
    interpolation) with `{param}`, then drop the `$`/`@` prefix and surrounding quotes."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "{":
            if depth == 0:
                out.append("{param}")
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).lstrip("$@").strip('"')


def _call_reference(node) -> RawCall:
    if node.type == "member_access_expression":
        name_node = node.child_by_field_name("name")
        name = name_node.text.decode() if name_node is not None else node.text.decode()
        receiver_node = node.child_by_field_name("expression")
        receiver = None
        if receiver_node is not None and receiver_node.type in ("identifier", "this"):
            receiver = receiver_node.text.decode()
        return RawCall(name=name, receiver=receiver)
    if node.type == "generic_name":
        inner = node.children[0]
        return _call_reference(inner) if inner.type == "member_access_expression" else RawCall(name=inner.text.decode())
    return RawCall(name=node.text.decode())


def _type_text(node) -> str | None:
    return node.text.decode(errors="replace") if node is not None else None
