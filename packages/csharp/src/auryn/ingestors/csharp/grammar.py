"""tree-sitter-c-sharp Language/Parser setup, in one place so file_parser.py and
tests always get the exact same configured parser.

Unlike PL/SQL's ANTLR grammar, tree-sitter-c-sharp ships a pre-built PyPI package --
no codegen/vendoring needed (see the C# ingestor plan for why this was chosen over
Roslyn)."""

from __future__ import annotations

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

LANGUAGE = Language(tscsharp.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
