"""tree-sitter-kotlin Language/Parser setup, in one place so file_parser.py and tests always
get the exact same configured parser. Like tree-sitter-java/-rust, it ships a pre-built PyPI
package (`tree-sitter-kotlin`, abi3 wheels) exposing a `language()` entry point -- no
codegen/vendoring."""

from __future__ import annotations

import tree_sitter_kotlin as tsk
from tree_sitter import Language, Parser

LANGUAGE = Language(tsk.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
