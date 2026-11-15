"""tree-sitter-bash Language/Parser setup, in one place so file_parser.py and tests always
get the exact same configured parser. Like tree-sitter-go/-rust/-java, it ships a pre-built
PyPI package -- no codegen/vendoring (contrast PL/SQL's vendored ANTLR grammar). The language
entry point is `tree_sitter_bash.language()` (verified against tree-sitter-bash 0.25.1)."""

from __future__ import annotations

import tree_sitter_bash as tsbash
from tree_sitter import Language, Parser

LANGUAGE = Language(tsbash.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
