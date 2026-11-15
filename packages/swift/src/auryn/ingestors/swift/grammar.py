"""tree-sitter-swift Language/Parser setup, in one place so file_parser.py and tests always
get the exact same configured parser. Like tree-sitter-java/-rust/-c, it ships a pre-built PyPI
package -- no codegen/vendoring (contrast PL/SQL's vendored ANTLR grammar). The distribution
exposes a plain `language()` factory (confirmed against tree-sitter-swift 0.7.3)."""

from __future__ import annotations

import tree_sitter_swift as tss
from tree_sitter import Language, Parser

LANGUAGE = Language(tss.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
