"""tree-sitter-php Language/Parser setup, in one place so file_parser.py and tests always get
the exact same configured parser. Like tree-sitter-java/-c-sharp it ships a pre-built PyPI
package -- no codegen/vendoring.

NOTE: tree-sitter-php does NOT expose a bare `language()`. The package bundles two grammars:
`language_php()` -- full PHP including the interleaved text/HTML template grammar, i.e. source
starting with `<?php` -- and `language_php_only()` (PHP-only, no HTML). Real `.php` source files
are template-aware (`<?php ... ?>` islands in HTML), so we use `language_php()`."""

from __future__ import annotations

import tree_sitter_php as tsphp
from tree_sitter import Language, Parser

LANGUAGE = Language(tsphp.language_php())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
