"""tree-sitter-typescript Language/Parser setup, in one place so file_parser.py and tests
always get the exact same configured parser. Like tree-sitter-c/-c-sharp, it ships a
pre-built PyPI package -- no codegen/vendoring (contrast PL/SQL's vendored ANTLR grammar).

One package, two grammar variants:
- `.ts`                       -> the `typescript` grammar (handles `<T>` type assertions /
                                 generics that the JSX grammar would misparse).
- everything else (`.tsx`,    -> the `tsx` grammar, a superset that also parses JSX *and*
  `.jsx`, `.js`, `.mjs`,        plain JS (JS is TS minus the type annotations). Using it for
  `.cjs`)                       `.js` means a React component written in a `.js` file still
                                 parses.

The suffix also decides the recorded `dialect` ("ts"/"js") and whether the file is a `.d.ts`
type-declaration file -- both surfaced so the graph keeps the TS-vs-JS distinction without
splitting into two languages (see plugin.py)."""

from __future__ import annotations

import tree_sitter_typescript as tst
from tree_sitter import Language, Parser

_TS = Language(tst.language_typescript())
_TSX = Language(tst.language_tsx())

# Suffixes that must go through the plain `typescript` grammar; all other owned suffixes use
# `tsx`. Kept narrow so a new JSX-bearing suffix defaults to the safe (tsx) parser.
_TS_ONLY_SUFFIXES = (".ts",)
_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")


def _suffix(path: str) -> str:
    path = path.replace("\\", "/")
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def new_parser(path: str) -> Parser:
    """A Parser configured for `path`'s grammar variant (see module docstring)."""
    lang = _TS if _suffix(path) in _TS_ONLY_SUFFIXES else _TSX
    return Parser(lang)


def dialect_for(path: str) -> str:
    """'ts' for .ts/.tsx, 'js' for .js/.jsx/.mjs/.cjs -- the recorded provenance of how much
    type information the file can carry (JS annotations are absent -> weaker call resolution)."""
    return "js" if _suffix(path) in _JS_SUFFIXES else "ts"


def is_declaration(path: str) -> bool:
    """True for a `.d.ts` type-declaration file: no runtime, but it declares the types/
    signatures other modules reference."""
    return path.replace("\\", "/").lower().endswith(".d.ts")
