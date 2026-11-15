"""tree-sitter-go Language/Parser setup, in one place so file_parser.py and tests always
get the exact same configured parser. Like tree-sitter-java/-c/-c-sharp, it ships a pre-built
PyPI package -- no codegen/vendoring (contrast PL/SQL's vendored ANTLR grammar)."""

from __future__ import annotations

import tree_sitter_go as tsgo
from tree_sitter import Language, Parser

LANGUAGE = Language(tsgo.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
