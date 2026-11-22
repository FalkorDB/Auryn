"""Go HTTP idioms (ADR 0006), read from one function body's tree-sitter nodes: the requests it SENDS
(client side -> `RawHttpCall`, which resolution turns into `CALLS_API` to a route-keyed placeholder)
and the routes it REGISTERS (server side -> `RawRoute`, which the go-routes enrichment turns into an
`APIEndpoint` exposed by the handler). Called from file_parser only when the file imports `net/http`
or a router package, so other files pay nothing.

Client side -- a call counts only when its receiver is known to be net/http:
- `http.Get/Head/Post/PostForm(url, ...)` and `http.DefaultClient.Get(url)`;
- `c.Get/Head/Post/PostForm(url, ...)` where `c` is a parameter typed `*http.Client` or a local set from
  `&http.Client{...}`, `http.Client{...}`, `new(http.Client)` or `http.DefaultClient`;
- `http.NewRequest(verb, url, ...)` / `http.NewRequestWithContext(ctx, verb, url, ...)`. This is how a
  `client.Do(req)` flow is covered: `Do` carries neither verb nor URL, the request it sends is built here.
The URL is a string literal, a `fmt.Sprintf` format or a `+` chain; each dynamic part becomes `{param}`
(like a TS template string or a C# interpolation). A URL with no `/` in it is dropped.

Server side -- one `RawRoute` per (verb, route):
- net/http and anything ServeMux-shaped: `X.HandleFunc(pattern, h)` / `X.Handle(pattern, h)`, with Go 1.22
  patterns (`"POST /api/orders/{id}"`, a host prefix, `{name...}`, `{$}`); gorilla/mux's chained
  `.Methods("POST", ...)` gives the verbs;
- gin `X.GET/POST/.../Any(path, handlers...)` and `X.Handle(verb, path, handlers...)` (the handler is the
  LAST argument, middleware comes first);
- echo `X.GET/.../Any(path, h, middleware...)` and `X.Add(verb, path, h, ...)` (the handler is the FIRST);
- chi `X.Get/Post/...(path, h)`, `X.Method/MethodFunc(verb, path, h)`.
A group prefix is joined in when it is set up in the SAME function: gin/echo `g := r.Group("/api")`,
gorilla `s := r.PathPrefix("/api").Subrouter()`, chi `r.Route("/api", func(r chi.Router) {...})` and
`r.Group(func(r chi.Router) {...})`. A router that arrives as a parameter, a field, or through chi's
`Mount` / net/http's `StripPrefix` keeps its written path -- its prefix is set up elsewhere."""

from __future__ import annotations

import re
from dataclasses import dataclass

from auryn.core import http_seam
from auryn.ingestors.go.raw_model import RawHttpCall, RawImport, RawParameter, RawReceiver, RawRoute

_NET_HTTP = "net/http"
_ROUTER_ROOTS = {
    "github.com/gin-gonic/gin": "gin",
    "github.com/labstack/echo": "echo",
    "github.com/go-chi/chi": "chi",
    "github.com/gorilla/mux": "gorilla",
}
_STRING_LITERALS = ("interpreted_string_literal", "raw_string_literal")

_CLIENT_VERBS = {"Get": "GET", "Head": "HEAD", "Post": "POST", "PostForm": "POST"}
_STD_VERBS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS", "TRACE"})
_UPPER_ROUTE_METHODS = _STD_VERBS | {"Any"}                                      # gin / echo
_TITLE_ROUTE_METHODS = {v.title(): v for v in _STD_VERBS}                        # chi
# Wrappers that hand back the handler they are given, and which argument that is.
_HTTP_WRAPPERS = {"HandlerFunc": 0, "StripPrefix": 1, "TimeoutHandler": 0, "MaxBytesHandler": 0}
_ROUTER_WRAPPERS = {"gin": {"WrapF": 0, "WrapH": 0}, "echo": {"WrapHandler": 0}}

_FMT_VERB = re.compile(r"%[-+# 0]*(?:\[\d+\])?(?:\d+|\*)?(?:\.(?:\d+|\*)?)?[a-zA-Z]")
_GO_PATTERN = re.compile(r"^([A-Z]+)\s+(\S.*)$")
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_FUNC_VALUE = "func"  # var_types marker: the variable holds a func literal (an inline handler)


@dataclass(frozen=True)
class HttpImports:
    """What a file imports that matters here: every alias -> import path, the aliases naming
    `net/http` and `fmt`, and which router packages are present (keyed by the names in
    _ROUTER_ROOTS, a major-version suffix like `/v5` ignored)."""

    aliases: dict
    http: frozenset
    fmt: frozenset
    routers: frozenset

    @property
    def any(self) -> bool:
        return bool(self.http or self.routers)


def package_name(import_path: str) -> str:
    """The name a non-aliased import binds: the last path segment, skipping a major-version segment
    (`github.com/go-chi/chi/v5` -> `chi`) and a gopkg.in `.vN` suffix (`gopkg.in/yaml.v3` -> `yaml`)."""
    segs = [s for s in import_path.split("/") if s]
    if not segs:
        return ""
    name = segs[-1]
    if re.fullmatch(r"v\d+", name) and len(segs) > 1:
        name = segs[-2]
    return re.sub(r"\.v\d+$", "", name)


def import_context(imports: list[RawImport]) -> HttpImports:
    aliases: dict = {}
    for imp in imports:
        alias = imp.alias or package_name(imp.path)
        if alias and alias not in ("_", "."):
            aliases[alias] = imp.path
    routers = set()
    for path in aliases.values():
        root = re.sub(r"/v\d+$", "", path)
        if root in _ROUTER_ROOTS:
            routers.add(_ROUTER_ROOTS[root])
    return HttpImports(
        aliases=aliases,
        http=frozenset(a for a, p in aliases.items() if p == _NET_HTTP),
        fmt=frozenset(a for a, p in aliases.items() if p == "fmt"),
        routers=frozenset(routers),
    )


# -- shared node helpers -------------------------------------------------------------------------

def _txt(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace") if node is not None else ""


def _args(call) -> list:
    al = call.child_by_field_name("arguments")
    return [a for a in al.named_children if a.type != "comment"] if al is not None else []


def _selector(call):
    """(operand node, field name) when `call` is `operand.field(...)`, else (None, None)."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return None, None
    field = fn.child_by_field_name("field")
    return fn.child_by_field_name("operand"), (field.text.decode() if field is not None else None)


def _string(node, src: bytes) -> str | None:
    """The value of a string literal node (quotes dropped), else None."""
    if node is None or node.type not in _STRING_LITERALS:
        return None
    return _txt(node, src)[1:-1]


def _is_ident(node, names) -> bool:
    return node is not None and node.type == "identifier" and node.text.decode() in names


def _verb(node, src: bytes, ctx: HttpImports) -> str | None:
    """A verb argument: a string literal (`"POST"`) or a net/http constant (`http.MethodPost`)."""
    lit = _string(node, src)
    if lit is not None:
        return lit.upper() or None
    if node is not None and node.type == "selector_expression":
        operand, field = node.child_by_field_name("operand"), node.child_by_field_name("field")
        name = field.text.decode() if field is not None else ""
        if _is_ident(operand, ctx.http) and name.startswith("Method"):
            return name[len("Method"):].upper() or None
    return None


# -- client side ---------------------------------------------------------------------------------

def _url(node, src: bytes, ctx: HttpImports) -> str | None:
    """The request URL an argument spells, with each dynamic part as `{param}`; None when nothing of
    it is literal (a bare variable carries no route)."""
    parts: list[tuple[str, bool]] = []  # (text, is_literal)

    def collect(n) -> None:
        if n.type == "parenthesized_expression" and n.named_children:
            collect(n.named_children[0])
            return
        lit = _string(n, src)
        if lit is not None:
            parts.append((lit, True))
            return
        if n.type == "binary_expression":
            op = n.child_by_field_name("operator")
            if op is not None and op.type == "+":
                collect(n.child_by_field_name("left"))
                collect(n.child_by_field_name("right"))
                return
        if n.type == "call_expression":
            operand, field = _selector(n)
            args = _args(n)
            fmt_lit = _string(args[0], src) if args else None
            if field == "Sprintf" and _is_ident(operand, ctx.fmt) and fmt_lit is not None:
                parts.append((_FMT_VERB.sub("{param}", fmt_lit.replace("%%", "\x00")).replace("\x00", "%"), True))
                return
        parts.append(("{param}", False))

    collect(node)
    if not any(is_lit for _, is_lit in parts):
        return None
    url = "".join(text for text, _ in parts)
    return url if http_seam.looks_like_route(url) else None


def _client_typed(type_text: str, ctx: HttpImports) -> bool:
    t = type_text.replace(" ", "").lstrip("*")
    return any(t == f"{a}.Client" for a in ctx.http)


def _client_value(node, ctx: HttpImports) -> bool:
    """A value expression that yields an `*http.Client`: `&http.Client{...}`, `http.Client{...}`,
    `new(http.Client)` or `http.DefaultClient`."""
    if node is None:
        return False
    if node.type == "unary_expression":
        return _client_value(node.child_by_field_name("operand"), ctx)
    if node.type == "composite_literal":
        return _client_typed(node.child_by_field_name("type").text.decode(), ctx)
    if node.type == "selector_expression":
        field = node.child_by_field_name("field")
        return _is_ident(node.child_by_field_name("operand"), ctx.http) and field is not None \
            and field.text.decode() == "DefaultClient"
    if node.type == "call_expression":
        fn, args = node.child_by_field_name("function"), _args(node)
        return _is_ident(fn, {"new"}) and len(args) == 1 and _client_typed(args[0].text.decode(), ctx)
    return False


def _bindings(node):
    """(name node, value node | None, type node | None) for every variable a declaration or an
    assignment binds -- `a, b := x, y`, `a = x`, `var a = x`, `var a T`."""
    if node.type in ("short_var_declaration", "assignment_statement"):
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        names = left.named_children if left is not None else []
        values = right.named_children if right is not None else []
        for i, name in enumerate(names):
            yield name, (values[i] if len(values) == len(names) else None), None
    elif node.type == "var_spec":
        names = [c for c in node.children if c.type == "identifier"]
        value = node.child_by_field_name("value")
        values = value.named_children if value is not None else []
        for i, name in enumerate(names):
            yield name, (values[i] if len(values) == len(names) else None), node.child_by_field_name("type")


def client_calls(body, src: bytes, ctx: HttpImports, params: list[RawParameter]) -> list[RawHttpCall]:
    """Every outbound request the body sends, deduped on (verb, url)."""
    if body is None or not ctx.http:
        return []
    clients = {p.name for p in params if p.name and _client_typed(p.type, ctx)}
    calls = []
    stack = [body]
    while stack:  # pre-pass: locals holding an *http.Client
        n = stack.pop()
        if n.type in ("short_var_declaration", "assignment_statement", "var_spec"):
            for name, value, typ in _bindings(n):
                if name.type == "identifier" and (_client_value(value, ctx)
                                                  or (typ is not None and _client_typed(typ.text.decode(), ctx))):
                    clients.add(name.text.decode())
        elif n.type == "call_expression":
            calls.append(n)
        stack.extend(n.children)

    out: list[RawHttpCall] = []
    seen: set = set()
    for call in sorted(calls, key=lambda c: c.start_byte):
        operand, field = _selector(call)
        if operand is None:
            continue
        args = _args(call)
        verb, url_node = None, None
        if field in _CLIENT_VERBS and args and (
            _is_ident(operand, ctx.http) or _is_ident(operand, clients) or _client_value(operand, ctx)
        ):
            verb, url_node = _CLIENT_VERBS[field], args[0]
        elif field == "NewRequest" and _is_ident(operand, ctx.http) and len(args) >= 2:
            verb, url_node = _verb(args[0], src, ctx) or "ANY", args[1]
        elif field == "NewRequestWithContext" and _is_ident(operand, ctx.http) and len(args) >= 3:
            verb, url_node = _verb(args[1], src, ctx) or "ANY", args[2]
        if url_node is None:
            continue
        url = _url(url_node, src, ctx)
        if url is not None and (verb, url) not in seen:
            seen.add((verb, url))
            out.append(RawHttpCall(url=url, method=verb))
    return out


# -- server side ---------------------------------------------------------------------------------

def _param_segment(seg: str) -> str:
    """One route segment with the framework's param syntax rendered `{name}` (the stitch's template
    form): gin/echo `:id` and `*path`, chi/gorilla `{id:[0-9]+}`, Go 1.22 `{path...}`; `{$}` drops."""
    if seg == "{$}":
        return ""
    if seg.startswith(":") and len(seg) > 1:
        return "{" + seg[1:] + "}"
    if seg.startswith("*"):
        return "{" + (seg[1:] or "path") + "}"
    if seg.startswith("{") and seg.endswith("}"):
        name = seg[1:-1].split(":", 1)[0].removesuffix("...")
        return "{" + (name or "param") + "}"
    return seg


def route_of(prefix: str, path: str) -> str:
    """Join a group prefix and a registered path into one normalized route."""
    segs = [_param_segment(s) for p in (prefix, path) for s in p.split("/") if s]
    return http_seam.normalize_route("/" + "/".join(s for s in segs if s))


def _go_pattern(pattern: str) -> tuple[str, str] | None:
    """(verb, path) of a ServeMux pattern: Go 1.22's `"POST /x"` gives the verb (else ANY) and a host
    before the path (`"api.example.com/x"`) is dropped. None when there is no path at all."""
    m = _GO_PATTERN.match(pattern.strip())
    verb, rest = (m.group(1), m.group(2)) if m and m.group(1) in _STD_VERBS else ("ANY", pattern.strip())
    slash = rest.find("/")
    if slash < 0:
        return None
    return verb, rest[slash:]


class _RouteWalker:
    """An in-order walk of one body that tracks the group prefix each router variable carries and the
    type of each variable a handler may be read from (receiver, params, `x := &T{}` locals)."""

    def __init__(self, src: bytes, ctx: HttpImports, var_types: dict):
        self.src = src
        self.ctx = ctx
        self.var_types = var_types
        self.routes: list[RawRoute] = []

    def walk(self, node, prefixes: dict) -> None:
        for child in node.children:
            self.visit(child, prefixes)

    def visit(self, n, prefixes: dict) -> None:
        if n.type in ("short_var_declaration", "assignment_statement", "var_spec"):
            for name, value, typ in _bindings(n):
                self._bind(name, value, typ, prefixes)
            self.walk(n, prefixes)
        elif n.type == "call_expression":
            if not self._call(n, prefixes):
                self.walk(n, prefixes)
        elif n.type == "func_literal":
            self.walk(n, dict(prefixes))  # a closure's own `r :=` must not leak out of it
        else:
            self.walk(n, prefixes)

    def _bind(self, name, value, typ, prefixes: dict) -> None:
        if name.type != "identifier":
            return
        var = name.text.decode()
        prefix = self._group_prefix(value, prefixes) if value is not None else None
        if prefix is not None:
            prefixes[var] = prefix
        else:
            prefixes.pop(var, None)
        lit = value.child_by_field_name("operand") if value is not None and value.type == "unary_expression" \
            else value
        if value is not None and value.type == "func_literal":
            self.var_types[var] = _FUNC_VALUE
        elif lit is not None and lit.type == "composite_literal":
            self.var_types[var] = _txt(lit.child_by_field_name("type"), self.src)
        elif typ is not None:
            self.var_types[var] = _txt(typ, self.src)
        elif value is not None:
            self.var_types[var] = ""  # set from a call or another value: its type is unknown

    def _prefix(self, node, prefixes: dict) -> str:
        """The group prefix a router expression carries ("" when none is known)."""
        if node is None:
            return ""
        if node.type == "identifier":
            return prefixes.get(node.text.decode(), "")
        return self._group_prefix(node, prefixes) or ""

    def _group_prefix(self, node, prefixes: dict) -> str | None:
        """The prefix of a router-producing call -- gin/echo `X.Group("/p")`, gorilla
        `X.PathPrefix("/p").Subrouter()`, chi `X.With(mw)` -- else None."""
        if node is None or node.type != "call_expression":
            return None
        operand, field = _selector(node)
        args = _args(node)
        if field == "Group" and args and self.ctx.routers & {"gin", "echo"}:
            path = _string(args[0], self.src)
            return None if path is None else _join(self._prefix(operand, prefixes), path)
        if field == "Subrouter" and "gorilla" in self.ctx.routers and operand is not None \
                and operand.type == "call_expression":
            inner_operand, inner_field = _selector(operand)
            inner_args = _args(operand)
            if inner_field == "PathPrefix" and inner_args:
                path = _string(inner_args[0], self.src)
                return None if path is None else _join(self._prefix(inner_operand, prefixes), path)
        if field == "With" and "chi" in self.ctx.routers:
            return self._prefix(operand, prefixes)
        return None

    def _call(self, call, prefixes: dict) -> bool:
        """Handle a route registration or a chi sub-router closure; True when it was one."""
        operand, field = _selector(call)
        if field is None:
            return False
        args = _args(call)
        if field == "Methods" and "gorilla" in self.ctx.routers and operand is not None \
                and operand.type == "call_expression":
            verbs = [v for v in (_verb(a, self.src, self.ctx) for a in args) if v]
            if self._register(operand, prefixes, verbs or None):
                return True
            return False
        if "chi" in self.ctx.routers and field in ("Route", "Group") and args \
                and args[-1].type == "func_literal":
            if field == "Route" and len(args) == 2 and _string(args[0], self.src) is not None:
                prefix = _join(self._prefix(operand, prefixes), _string(args[0], self.src))
            elif field == "Group" and len(args) == 1:
                prefix = self._prefix(operand, prefixes)
            else:
                return False
            inner = dict(prefixes)
            params = args[-1].child_by_field_name("parameters")
            first = next((p for p in params.named_children if p.type == "parameter_declaration"), None) \
                if params is not None else None
            name = first.child_by_field_name("name") if first is not None else None
            if name is not None:
                inner[name.text.decode()] = prefix
            body = args[-1].child_by_field_name("body")
            if body is not None:
                self.walk(body, inner)
            return True
        return self._register(call, prefixes, None)

    def _register(self, call, prefixes: dict, verbs: list[str] | None) -> bool:
        operand, field = _selector(call)
        found = self._registration(operand, field, _args(call))
        if found is None:
            return False
        reg_verbs, path, handler_node, framework = found
        handler = self._handler_ref(handler_node)
        if handler is not None:
            route = route_of(self._prefix(operand, prefixes), path)
            for verb in verbs or reg_verbs:
                self.routes.append(RawRoute(method=verb, route=route, handler=handler, framework=framework))
        return True

    def _registration(self, operand, field, args):
        """(verbs, path, handler node, framework) when `operand.field(args)` registers a route."""
        ctx, src = self.ctx, self.src
        if field is None or not args:
            return None
        first = _string(args[0], src)
        if field in ("HandleFunc", "Handle"):
            second = _string(args[1], src) if len(args) > 1 else None
            if field == "Handle" and "gin" in ctx.routers and len(args) >= 3 and first and second is not None:
                return [first.upper()], second, args[-1], "gin"  # gin: Handle(verb, path, handlers...)
            if first is None or len(args) != 2 or not (ctx.http or ctx.routers & {"chi", "gorilla"}):
                return None
            pattern = _go_pattern(first)
            if pattern is None:
                return None
            if _is_ident(operand, ctx.http):
                framework = "net/http"
            else:
                framework = next((f for f in ("gorilla", "chi") if f in ctx.routers), "net/http")
            return [pattern[0]], pattern[1], args[1], framework
        if field in _UPPER_ROUTE_METHODS and ctx.routers & {"gin", "echo"}:
            if not _route_path(first) or len(args) < 2:
                return None
            verb = "ANY" if field == "Any" else field
            if "gin" in ctx.routers and "echo" not in ctx.routers:
                return [verb], first, args[-1], "gin"  # gin: middleware first, the handler last
            if "echo" in ctx.routers and "gin" not in ctx.routers:
                return [verb], first, args[1], "echo"  # echo: the handler first, middleware after
            return ([verb], first, args[1], "gin") if len(args) == 2 else None
        if field == "Add" and "echo" in ctx.routers and len(args) >= 3:
            path = _string(args[1], src)
            verb = _verb(args[0], src, ctx)
            return ([verb], path, args[2], "echo") if verb and _route_path(path) else None
        if field in _TITLE_ROUTE_METHODS and "chi" in ctx.routers and len(args) == 2 \
                and not _is_ident(operand, ctx.http) and _route_path(first):
            return [_TITLE_ROUTE_METHODS[field]], first, args[1], "chi"
        if field in ("Method", "MethodFunc") and "chi" in ctx.routers and len(args) == 3:
            path = _string(args[1], src)
            verb = _verb(args[0], src, ctx)
            return ([verb], path, args[2], "chi") if verb and _route_path(path) else None
        return None

    def _type_ref(self, type_text: str) -> str | None:
        """`Type` (this package) or `<import path>.Type` for a written type; None for a type from
        net/http or a router package, or one that isn't a plain named type."""
        t = re.sub(r"\[.*\]$", "", type_text.replace(" ", "").lstrip("*&"))
        if "." in t:
            qualifier, name = t.split(".", 1)
            path = self.ctx.aliases.get(qualifier)
            if path is None or path == _NET_HTTP or re.sub(r"/v\d+$", "", path) in _ROUTER_ROOTS \
                    or not _IDENT.match(name):
                return None
            return f"{path}.{name}"
        return t if _IDENT.match(t) else None

    def _handler_ref(self, node) -> str | None:
        """The handler argument as RawRoute.handler, or None when it can't be named (a constructor
        call, a middleware chain, a variable of unknown origin holding a handler)."""
        if node is None:
            return None
        t = node.type
        if t == "parenthesized_expression" and node.named_children:
            return self._handler_ref(node.named_children[0])
        if t == "func_literal":
            return "inline"
        if t == "identifier":
            name = node.text.decode()
            if name not in self.var_types:
                return f"func:{name}"
            if self.var_types[name] == _FUNC_VALUE:
                return "inline"
            typ = self._type_ref(self.var_types[name])
            return f"method:{typ}.ServeHTTP" if typ else None  # `h := &OrdersHandler{}` -> its ServeHTTP
        if t == "selector_expression":
            operand, field = node.child_by_field_name("operand"), node.child_by_field_name("field")
            name = field.text.decode() if field is not None else ""
            if operand is not None and operand.type == "identifier":
                var = operand.text.decode()
                if var in self.var_types:
                    typ = self._type_ref(self.var_types[var])
                    return f"method:{typ}.{name}" if typ else f"method:{name}"
                path = self.ctx.aliases.get(var)
                if path is not None:
                    return None if path == _NET_HTTP else f"func:{path}.{name}"
            return f"method:{name}"
        if t in ("unary_expression", "composite_literal"):
            lit = node.child_by_field_name("operand") if t == "unary_expression" else node
            if lit is not None and lit.type == "composite_literal":
                typ = self._type_ref(_txt(lit.child_by_field_name("type"), self.src))
                return f"method:{typ}.ServeHTTP" if typ else None
            return None
        if t == "call_expression":
            operand, field = _selector(node)
            args = _args(node)
            if operand is not None and operand.type == "identifier":
                path = self.ctx.aliases.get(operand.text.decode())
                wrappers = _HTTP_WRAPPERS if path == _NET_HTTP else \
                    _ROUTER_WRAPPERS.get(_ROUTER_ROOTS.get(re.sub(r"/v\d+$", "", path or ""), ""), {})
                index = wrappers.get(field)
                if index is not None and len(args) > index:
                    return self._handler_ref(args[index])
        return None


def _route_path(path: str | None) -> bool:
    """A router method's path argument: "" (the group root) or an absolute path."""
    return path is not None and (path == "" or path.startswith("/"))


def _join(prefix: str, path: str) -> str:
    return "/".join(p.strip("/") for p in (prefix, path) if p and p.strip("/"))


def routes(body, src: bytes, ctx: HttpImports, params: list[RawParameter],
           receiver: RawReceiver | None) -> list[RawRoute]:
    """Every route the body registers, in source order."""
    if body is None or not ctx.any:
        return []
    var_types = {p.name: p.type for p in params if p.name}
    if receiver is not None and receiver.name:
        var_types[receiver.name] = receiver.type_name
    walker = _RouteWalker(src, ctx, var_types)
    walker.walk(body, {})
    return walker.routes
