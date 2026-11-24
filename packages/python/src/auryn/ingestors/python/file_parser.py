"""Phase 1 of the `ast` fallback engine: parse one .py file into the raw model.

Pure syntax, stdlib `ast` only -- no type inference, no cross-file work (that's
resolution.py). A file that doesn't parse (syntax error) yields an empty module rather
than aborting the ingest, mirroring the endpoint's "one bad subgraph never aborts the
source" contract.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import os
import signal
import threading

from auryn.core.docstrings import leading_comment_from_source
from auryn.ingestors.python import ids
from auryn.ingestors.python.raw_model import (
    RawAttribute,
    RawCall,
    RawClass,
    RawFunction,
    RawImport,
    RawModule,
    RawParameter,
)

logger = logging.getLogger(__name__)


def _max_parse_seconds() -> int:
    """Per-file parse budget (SIGALRM, Unix only). A pathological file -- typically a huge
    generated module -- is skipped instead of stalling the whole ingest. Read from the env each
    call so it's overridable per run; 0/negative disables the cap. Mirrors the PL/SQL ingestor's
    --max-parse-seconds guard (see plsql/file_ingestor.parse_plsql_file)."""
    try:
        return int(os.environ.get("AURYN_PY_MAX_PARSE_SECONDS", "30"))
    except ValueError:
        return 30


class _ParseTimeout(BaseException):
    """Raised from the per-file SIGALRM handler. A BaseException (not Exception) so the
    per-node parse work's own error handling can't accidentally swallow it."""


@contextlib.contextmanager
def _parse_deadline(max_seconds: int):
    """Bound the wrapped parse to `max_seconds` via SIGALRM, restoring any prior handler after.
    A no-op where the cap is disabled, SIGALRM is unavailable (Windows), or we're off the main
    thread -- signal.signal() only works on the main thread, and the deep=background path parses
    in a worker thread. So behaviour is unchanged except a would-be-hanging file on the main
    thread (the fast serial path, and future pool workers' own main thread) is interrupted."""
    if (
        max_seconds <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def _on_timeout(signum, frame):
        raise _ParseTimeout

    previous = signal.signal(signal.SIGALRM, _on_timeout)
    signal.alarm(int(max_seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _line_starts(data: bytes) -> list[int]:
    """Byte offset of the start of each line in `data` (0-based line index -> byte position).
    Computed once per file so source extraction is O(1) per declaration instead of re-scanning."""
    starts = [0]
    i = data.find(b"\n")
    while i != -1:
        starts.append(i + 1)
        i = data.find(b"\n", i + 1)
    return starts


class _Source:
    """A file's text prepared once for cheap, repeated source extraction.

    `ast.get_source_segment` and `leading_comment_from_source` each re-split the whole file on
    every call -- O(file) per declaration, i.e. O(n^2) over a module (373s on a 162k-line generated
    file; see the parse-hang fix). This precomputes line byte-offsets and the split line list once,
    turning both into O(1)-setup lookups. `segment` mirrors ast.get_source_segment's byte-offset
    semantics (CPython col_offset is a UTF-8 byte offset)."""

    __slots__ = ("text", "_data", "_starts", "_lines")

    def __init__(self, text: str) -> None:
        self.text = text
        self._data = text.encode("utf-8")
        self._starts = _line_starts(self._data)
        self._lines = text.splitlines()  # reused by leading_comment (no keepends, matches docstrings)

    def segment(self, node: ast.AST) -> str:
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if end_lineno is None or end_col is None:
            return ""
        start = self._starts[node.lineno - 1] + node.col_offset
        end = self._starts[end_lineno - 1] + end_col
        return self._data[start:end].decode("utf-8", "replace")

    def leading_comment(self, top_line: int) -> str:
        return leading_comment_from_source(self.text, top_line, lines=self._lines)


def parse_module(source: bytes | str, path: str, package_dirs: frozenset[str] | None = None) -> RawModule:
    dotted = ids.dotted_module_path(path, package_dirs)
    module = RawModule(path=path, dotted=dotted, is_package=ids.is_package_init(path))
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    if text.startswith("\ufeff"):
        # Python accepts a leading UTF-8 BOM, but ast.parse() on a str rejects it with
        # "invalid non-printable character U+FEFF", which would skip the whole file. Drop the
        # single leading BOM so BOM-prefixed source is parsed. _Source sees the same stripped
        # text below, so byte offsets/segments stay consistent.
        text = text[1:]
    try:
        with _parse_deadline(_max_parse_seconds()):
            tree = ast.parse(text, filename=path)
            src = _Source(text)
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module.imports.extend(_imports(node, dotted, module.is_package))
                elif isinstance(node, ast.ClassDef):
                    module.classes.append(_class(node, src))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    module.functions.append(_function(node, src))
            module.calls = _module_calls(tree)
        return module
    except SyntaxError as e:
        # Broken (mid-edit) code: flag it so the resync can preserve the file's last-good
        # subgraph instead of wiping it to this empty module. See core/incremental_scoped.
        logger.warning("Skipping %s: could not parse (%s)", path, e)
        module.parse_ok = False
        return module
    except _ParseTimeout:
        # Pathological (usually generated) file: skip rather than stall the whole ingest.
        # Return a fresh empty module marked broken -- same contract as a syntax error, so the
        # resync preserves the file's last-good subgraph instead of half-parsed noise.
        lines = text.count("\n") + 1
        logger.warning(
            "Skipping %s: parse exceeded %ss (%d lines) -- likely generated; marked broken",
            path, _max_parse_seconds(), lines,
        )
        broken = RawModule(path=path, dotted=dotted, is_package=module.is_package)
        broken.parse_ok = False
        return broken


def parse_blob(args: tuple[str, bytes, frozenset[str] | None]) -> RawModule:
    """ProcessPoolExecutor worker: parse one ``(path, source_bytes, package_dirs)`` tuple. A
    module-level function (so it's picklable for the pool) living in this light module (so a
    spawned worker imports cheaply -- not the heavy git_sync). It runs in the worker's own main
    thread, so ``parse_module``'s per-file SIGALRM timeout still applies (the guard only no-ops
    off the main thread). Mirrors the C# ingestor's ``_parse_blob`` worker."""
    path, data, package_dirs = args
    return parse_module(data, path, package_dirs)


# -- imports -----------------------------------------------------------------------


def _imports(node: ast.Import | ast.ImportFrom, current_dotted: str, is_package: bool) -> list[RawImport]:
    out: list[RawImport] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            out.append(RawImport(local=local, target=alias.name, is_module=True))
        return out

    base = _resolve_from_base(node, current_dotted, is_package)
    for alias in node.names:
        if alias.name == "*":
            continue
        target = f"{base}.{alias.name}" if base else alias.name
        out.append(RawImport(local=alias.asname or alias.name, target=target, is_module=False))
    return out


def _resolve_from_base(node: ast.ImportFrom, current_dotted: str, is_package: bool) -> str:
    """Absolute dotted prefix a `from ... import` names hang off, resolving relative
    (`level > 0`) imports against the current module's package."""
    if not node.level:
        return node.module or ""
    # The current module's package: for a regular module a.b.c it's a.b; for a package's
    # own __init__ (dotted already == the package) it's the package itself.
    package = current_dotted if is_package else current_dotted.rsplit(".", 1)[0] if "." in current_dotted else ""
    # level 1 = current package, each extra level strips one more trailing segment.
    for _ in range(node.level - 1):
        package = package.rsplit(".", 1)[0] if "." in package else ""
    if node.module:
        return f"{package}.{node.module}" if package else node.module
    return package


# -- classes & functions -----------------------------------------------------------


def _decl_top_line(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """The declaration's first source line -- above any decorators, since a leading comment
    sits above the whole `@decorator`/`def` block, not between the decorators and the def."""
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _class(node: ast.ClassDef, src: _Source) -> RawClass:
    cls = RawClass(
        name=node.name,
        bases=[ast.unparse(b) for b in node.bases],
        decorators=[ast.unparse(d) for d in node.decorator_list],
        line_start=node.lineno,
        line_end=node.end_lineno or node.lineno,
        source=src.segment(node),
        doc=src.leading_comment(_decl_top_line(node)),
    )
    for stmt in node.body:
        if isinstance(stmt, ast.ClassDef):
            cls.nested.append(_class(stmt, src))
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cls.methods.append(_function(stmt, src, in_class=True))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            cls.class_attrs.append(
                RawAttribute(stmt.target.id, ast.unparse(stmt.annotation), "class_body", True,
                             name_line=stmt.target.lineno, name_col=stmt.target.col_offset)
            )
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    cls.class_attrs.append(
                        RawAttribute(tgt.id, None, "class_body", True, name_line=tgt.lineno, name_col=tgt.col_offset)
                    )
    return cls


def _function(node: ast.FunctionDef | ast.AsyncFunctionDef, src: _Source, *, in_class: bool = False) -> RawFunction:
    fn = RawFunction(
        name=node.name,
        params=_params(node.args),
        returns=ast.unparse(node.returns) if node.returns else None,
        decorators=[ast.unparse(d) for d in node.decorator_list],
        is_async=isinstance(node, ast.AsyncFunctionDef),
        is_generator=_is_generator(node),
        line_start=node.lineno,
        line_end=node.end_lineno or node.lineno,
        calls=_calls(node),
        source=src.segment(node),
        doc=src.leading_comment(_decl_top_line(node)),
        nested=_nested_functions(node.body, src),
    )
    if in_class and fn.params:
        fn.self_attrs = _self_attrs(node, self_name=fn.params[0].name)
    return fn


def _nested_functions(body: list[ast.stmt], src: _Source) -> list[RawFunction]:
    """The functions declared inside a scope's OWN body -- so a nested `def` (a tool/handler
    registered inside a `register(app)` factory, a closure) becomes its own node and its calls
    aren't lost. Mirrors `_calls_in_body`'s scope rules: it finds defs at any statement depth in
    the body (inside `if`/`for`/...) but stops at a deeper `def`/`class` -- those own their inner
    defs. A nested `def` is always a plain function, never a method (even inside a method body)."""
    out: list[RawFunction] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(_function(child, src))  # its own nested defs are collected recursively there
                continue  # don't descend -- that scope owns its inner defs
            if isinstance(child, ast.ClassDef):
                continue  # a nested class's methods aren't this scope's nested functions
            walk(child)

    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_function(stmt, src))
            continue
        if isinstance(stmt, ast.ClassDef):
            continue
        walk(stmt)
    return out


def _params(args: ast.arguments) -> list[RawParameter]:
    params: list[RawParameter] = []
    pos = 0

    def add(arg: ast.arg, kind: str, has_default: bool):
        nonlocal pos
        params.append(
            RawParameter(
                name=arg.arg,
                annotation=ast.unparse(arg.annotation) if arg.annotation else None,
                kind=kind,
                position=pos,
                has_default=has_default,
                name_line=arg.lineno,
                name_col=arg.col_offset,
            )
        )
        pos += 1

    positional = list(args.posonlyargs) + list(args.args)
    n_defaults = len(args.defaults)
    first_default = len(positional) - n_defaults
    for i, arg in enumerate(args.posonlyargs):
        add(arg, "positional_only", i >= first_default)
    for j, arg in enumerate(args.args):
        add(arg, "positional_or_keyword", (len(args.posonlyargs) + j) >= first_default)
    if args.vararg:
        add(args.vararg, "var_positional", False)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        add(arg, "keyword_only", default is not None)
    if args.kwarg:
        add(args.kwarg, "var_keyword", False)
    return params


def _is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        # Don't descend into nested functions' yields.
        if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
    return False


def _raw_call(func: ast.expr) -> RawCall | None:
    """Build a RawCall from a call's `func` expression -- a bare `name()` or a `receiver.name()`
    attribute access; anything else (a call on a subscript/call result) yields None. Shared by
    the function-body and module-scope collectors so both name a call site identically."""
    if isinstance(func, ast.Name):
        return RawCall(name=func.id, receiver=None, is_simple_name=True, line=func.lineno, col=func.col_offset)
    if isinstance(func, ast.Attribute):
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        end_col = func.end_col_offset or 0
        return RawCall(
            name=func.attr, receiver=receiver, is_simple_name=False,
            line=func.end_lineno or func.lineno, col=max(end_col - len(func.attr), 0),
        )
    return None


def _calls_in_body(body: list[ast.stmt]) -> list[RawCall]:
    """Calls executed when a scope's OWN code runs: every call in these statements and the
    control-flow nested in them (if/for/while/with/try), but NOT inside a nested def/class/lambda
    body -- each of those is its own scope and owns its own calls.

    Shared by a module body and a function/method body. Because it walks the *body* only, a
    function's decorators and default-argument expressions -- which hang off the `def` node, not
    its body, and execute in the ENCLOSING scope at definition time (`@deco`, `def f(x=compute())`)
    -- are correctly excluded from the function's own calls. Document order; a call's arguments are
    descended into so `print(load())` captures both.

    A `lambda` body is NOT skipped: a lambda is anonymous and never becomes its own graph node, so
    the calls inside it (`observe(lambda: helper(x))` -> `helper`) would otherwise vanish. They're
    attributed to the enclosing scope -- EXCEPT a call to one of the lambda's OWN parameters, which is
    a local (`lambda helper: helper()` calls the arg, not a same-named function), so it's dropped.
    A nested `def`/`class` IS skipped here -- it gets its own node and its calls are collected there
    (see `_function`'s nested recursion)."""
    out: list[RawCall] = []

    def visit(node: ast.AST, bound: frozenset[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return  # a nested def/class -- its own node owns its calls
        if isinstance(node, ast.Lambda):
            # defaults run in THIS scope (at definition time); the body runs with the lambda's params
            # bound, so a call to one of those params is a local, not a project function.
            for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]:
                visit(default, bound)
            visit(node.body, bound | _lambda_param_names(node.args))
            return
        if isinstance(node, ast.Call):
            rc = _raw_call(node.func)
            # Drop a call bound to a lambda parameter: a simple-name call to it (`lambda f: f()`), OR a
            # `receiver.attr()` whose RECEIVER is the param (`lambda client: client.send()` -- the arg,
            # not a same-named import). An unbound receiver still resolves normally.
            if rc is not None and not (rc.is_simple_name and rc.name in bound) and not (rc.receiver and rc.receiver in bound):
                out.append(rc)
        for child in ast.iter_child_nodes(node):
            visit(child, bound)

    for stmt in body:
        visit(stmt, frozenset())
    return out


def _lambda_param_names(args: ast.arguments) -> frozenset[str]:
    """Every parameter name a lambda binds (positional, keyword-only, *args, **kwargs)."""
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return frozenset(names)


def _calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[RawCall]:
    """The calls a function/method body makes -- scope-respecting (see _calls_in_body): excludes
    the decorators/defaults on the `def` (they run in the enclosing scope) and any nested def's
    body (its own scope)."""
    return _calls_in_body(node.body)


def _module_calls(tree: ast.Module) -> list[RawCall]:
    """Call sites in module-scope (top-level) code -- what makes the Module node a CALLS source for
    a script's driver code (e.g. `main()` under `if __name__ == '__main__':`). Same scope rules as
    a function body."""
    return _calls_in_body(tree.body)


def _self_attrs(node: ast.FunctionDef | ast.AsyncFunctionDef, self_name: str) -> list[RawAttribute]:
    out: list[RawAttribute] = []

    def record(target: ast.expr, annotation: str | None):
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == self_name
        ):
            end_col = target.end_col_offset or 0
            out.append(
                RawAttribute(
                    target.attr, annotation, node.name, False,
                    name_line=target.end_lineno or target.lineno,
                    name_col=max(end_col - len(target.attr), 0),
                )
            )

    for stmt in ast.walk(node):
        if isinstance(stmt, ast.AnnAssign):
            record(stmt.target, ast.unparse(stmt.annotation))
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                record(tgt, None)
    return out
