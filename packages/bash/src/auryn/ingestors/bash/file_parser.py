"""parse_file(path) -> RawFile: the parallelizable phase-1 unit (pure function, no shared
state -- safe under a ProcessPoolExecutor; only picklable raw_model dataclasses are returned,
no tree-sitter node crosses the boundary). parse_source(source, path_label) holds the logic and
takes raw bytes, so a git blob can be parsed without touching disk.

Every node type / field name below was confirmed empirically against tree-sitter-bash 0.25.1
(see the package's grammar probe), not assumed:

- top level (`program` children): `comment` (incl. the `#!` shebang), `function_definition`,
  `command` (a simple command), `if_statement`/`for_statement`/`while_statement`/`pipeline`/
  `variable_assignment` (which may nest `command`s / `command_substitution`), etc.
- `function_definition`: field `name` (a `word`; both `foo() { ... }` and `function foo { ... }`
  spellings expose it) and field `body` (a `compound_statement` `{ ... }`, or a subshell `( ... )`).
- `command`: field `name` = `command_name`, whose sole child (for a static command) is a `word`;
  a command whose name involves an expansion (`$cmd`) has no plain `word` child -> unnameable.
- `source`/`.`: a `command` whose command word is `source` or `.`; the include path is the first
  argument child (`word` | `string` | `raw_string` | `concatenation`).

Calls are collected by walking a function body for `command` nodes (so a command inside a
`command_substitution`, pipeline, loop or conditional still counts), but the walk does NOT descend
into a *nested* `function_definition` -- those inner commands belong to the nested function, which
is captured as its own RawFunction."""

from __future__ import annotations

from pathlib import Path

from auryn.core.docstrings import leading_comment
from auryn.ingestors.bash.raw_model import RawCall, RawFile, RawFunction, RawSource

# Shell builtins / keywords: a bare call to one of these is never a project function or a real
# external command, so it gets no CALLS edge (the exact analog of Go's _BUILTIN_FUNCS). `source`
# and `.` are handled separately (they become IMPORTS, not calls) and are listed here too so they
# are never mistaken for a command call.
_BUILTIN_COMMANDS = frozenset(
    {
        "source", ".", ":", "true", "false",
        "echo", "printf", "print", "read", "mapfile", "readarray",
        "cd", "pwd", "pushd", "popd", "dirs",
        "local", "declare", "typeset", "readonly", "export", "unset", "set",
        "shift", "getopts", "return", "exit", "break", "continue",
        "eval", "exec", "trap", "wait", "kill", "jobs", "bg", "fg", "disown",
        "test", "[", "[[", "alias", "unalias", "type", "hash", "help",
        "command", "builtin", "enable", "let", "history", "fc", "bind",
        "caller", "complete", "compgen", "compopt", "logout", "times",
        "ulimit", "umask", "suspend", "shopt", "unalias",
    }
)

# Node types that mark a value/command as dynamic (a name/path we cannot resolve statically).
_EXPANSION_TYPES = frozenset(
    {"simple_expansion", "expansion", "command_substitution", "arithmetic_expansion", "process_substitution"}
)


def parse_file(path: str) -> RawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> RawFile:
    from auryn.ingestors.bash.grammar import new_parser

    tree = new_parser().parse(source)
    rf = RawFile(path=path_label)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace") if node else ""

    def lines(node) -> tuple[int, int]:
        return node.start_point[0] + 1, node.end_point[0] + 1

    def fieldn(node, name):
        return node.child_by_field_name(name) if node is not None else None

    def has_expansion(node) -> bool:
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in _EXPANSION_TYPES:
                return True
            stack.extend(n.children)
        return False

    def command_word(cmd) -> str | None:
        """The static command name of a `command` node -- the single `word` under its
        `command_name` field -- or None when the name is an expansion (`$cmd`) or otherwise not a
        plain word (unnameable, so no CALLS edge, mirroring Go's chained-selector calls)."""
        name_node = fieldn(cmd, "name")
        if name_node is None or name_node.type != "command_name":
            return None
        words = [c for c in name_node.children if c.type == "word"]
        if len(name_node.named_children) != 1 or len(words) != 1:
            return None
        return txt(words[0])

    def first_argument(cmd):
        """The first argument node of a `command` (the child after its `command_name`, skipping
        any leading variable assignments/redirects), or None. Compared by node *type*, not object
        identity: tree-sitter's Python bindings hand out fresh Node wrappers per access, so `is`
        against a field lookup never matches."""
        seen_name = False
        for c in cmd.children:
            if c.type == "command_name":
                seen_name = True
                continue
            if not seen_name:
                continue
            if c.type in ("word", "string", "raw_string", "concatenation", "expansion", "simple_expansion"):
                return c
        return None

    # -- calls ------------------------------------------------------------------------------

    def collect_calls(body) -> list[RawCall]:
        """Command invocations inside a function body (deduped by name). Walks the whole subtree
        so calls nested in pipelines/conditionals/loops/command-substitutions are captured, but
        stops at a nested `function_definition` (its calls belong to it, not the enclosing func)."""
        calls: list[RawCall] = []
        seen: set[str] = set()
        if body is None:
            return calls
        stack = [(body, True)]
        while stack:
            n, is_root = stack.pop()
            if not is_root and n.type == "function_definition":
                continue  # nested function's body belongs to the nested function
            if n.type == "command":
                name = command_word(n)
                if name and name not in _BUILTIN_COMMANDS and name not in seen:
                    seen.add(name)
                    ls, _ = lines(n)
                    calls.append(RawCall(name=name, line_start=ls))
            for c in n.children:
                stack.append((c, False))
        return calls

    def build_function(node) -> RawFunction:
        name_node = fieldn(node, "name")
        body = fieldn(node, "body")
        ls, le = lines(node)
        doc = _strip_shebang(leading_comment(node, source))
        return RawFunction(
            name=txt(name_node),
            calls=collect_calls(body),
            source=txt(node),
            doc=doc,
            line_start=ls,
            line_end=le,
        )

    # -- source / . includes ----------------------------------------------------------------

    def build_source(cmd) -> RawSource | None:
        arg = first_argument(cmd)
        if arg is None:
            return None
        dynamic = arg.type in _EXPANSION_TYPES or has_expansion(arg)
        raw = txt(arg)
        path = raw.strip().strip('"').strip("'")
        if not path:
            return None
        ls, _ = lines(cmd)
        return RawSource(path=path, is_dynamic=dynamic, line_start=ls)

    # -- whole-tree walk --------------------------------------------------------------------
    # Functions are found at ANY depth (bash allows a function defined inside another). `source`
    # directives are collected wherever they appear (top level or inside a function): a `source`
    # brings definitions into the file's scope regardless of where it sits, so it is modelled as a
    # file-level include.

    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        for child in n.children:
            if child.type == "function_definition":
                rf.functions.append(build_function(child))
            elif child.type == "command":
                cw = command_word(child)
                if cw in ("source", "."):
                    src = build_source(child)
                    if src is not None:
                        rf.sources.append(src)
            stack.append(child)

    return rf


def _strip_shebang(doc: str) -> str:
    """A `#!/usr/bin/env bash` shebang directly above a declaration is captured as a leading
    comment; `clean_comment` strips the `#`, leaving a line beginning with `!`. Drop such lines so
    a shebang never masquerades as a function's doc."""
    if not doc:
        return doc
    kept = [ln for ln in doc.split("\n") if not ln.lstrip().startswith("!")]
    return "\n".join(kept).strip()
