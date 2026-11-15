# ANTLR4-generated PL/SQL parser, vendored from antlr/grammars-v4 (Apache-2.0):
# https://github.com/antlr/grammars-v4/tree/master/sql/plsql
#
# Regeneration steps (ANTLR 4.13.2, matching the antlr4-python3-runtime version pinned
# in pyproject.toml -- the generated code and runtime must be the same version):
#   1. Fetch PlSqlLexer.g4, PlSqlParser.g4, and Python3/{PlSqlLexerBase,PlSqlParserBase}.py
#      from the repo above.
#   2. Run Python3/transformGrammar.py against the two .g4 files (rewrites Java-style
#      `this.` to `self.` in embedded lexer predicates/actions).
#   3. antlr -Dlanguage=Python3 -visitor -o out PlSqlLexer.g4 PlSqlParser.g4
#   4. Copy PlSqlLexer.py, PlSqlParser.py, PlSqlParserListener.py, PlSqlParserVisitor.py
#      from `out/`, plus the two *Base.py files from step 1, into this directory.
#      The .interp/.tokens files are build-time only and aren't needed here.
#   5. PlSqlParserBase.py (hand-written upstream, not ANTLR-generated) does flat
#      `from PlSqlLexer import ...` inside two methods, which breaks once this is a
#      properly-nested package rather than a sys.path-added flat directory. Patched
#      to the same `if "." in __name__: from .PlSqlLexer import ...` fallback the
#      generated files already use -- re-apply this after regenerating.
