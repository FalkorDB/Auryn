"""tree-sitter-ruby Language/Parser setup, in one place so file_parser.py and tests always
get the exact same configured parser. Like tree-sitter-go/-rust/-java, it ships a pre-built
PyPI package -- no codegen/vendoring (contrast PL/SQL's vendored ANTLR grammar).

NOTE: tree-sitter-ruby exposes a plain `language()` (unlike tree-sitter-php, which bundles two
grammars behind `language_php()`/`language_php_only()`). Verified via `dir(tree_sitter_ruby)`."""

from __future__ import annotations

import tree_sitter_ruby as tsrb
from tree_sitter import Language, Parser

LANGUAGE = Language(tsrb.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
