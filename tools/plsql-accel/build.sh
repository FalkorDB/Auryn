#!/usr/bin/env bash
# Build the optional PL/SQL parse+extract accelerator (module `plsql_accel`): it parses AND extracts
# entirely in C++ (ANTLR C++ runtime over the SAME vendored grammar) and returns only the small
# RawPlsqlFile as JSON -- the parse tree never crosses into Python. That's what makes it ~12x (vs the
# ~2.6x a tree-translating speedy-antlr build gives). source_analysis imports it opt-in and falls
# back to the pure-Python ANTLR runtime when it's absent, so it's a drop-in speedup, never required.
#
# Correctness gate: the C++ parser MUST come from the exact .g4 that produced our vendored Python
# parser, or the trees (and the extractor's rule/token assumptions) won't match. That grammar is
# antlr/grammars-v4 @ the pinned commit below -- verified to regenerate the identical 1209-rule
# parser we vendored. The extractor (src/plsql_accel.cpp) reproduces source_analysis.py's
# _DeclarationVisitor + _CallsAndTableRefsVisitor; the plsql test suite asserts byte-identical output.
#
# Requirements: java (ANTLR 4.13.2 jar), a C++17 compiler, python3.12 with setuptools. For the
# deployable Linux x86-64 wheel, run inside a manylinux container -- see README.md.
set -euo pipefail
cd "$(dirname "$0")"

GRAMMAR_SHA="b434a051c56d"   # antlr/grammars-v4 sql/plsql -- matches our vendored parser
ANTLR_VERSION="4.13.2"
WORK="${1:-./build}"
BASE="https://raw.githubusercontent.com/antlr/grammars-v4/${GRAMMAR_SHA}/sql/plsql"

mkdir -p "$WORK"/{gen,include}
cd "$WORK"

echo ">> 1. tools: ANTLR jar + C++ runtime + json single-header"
[ -f antlr.jar ] || curl -fsSL "https://www.antlr.org/download/antlr-${ANTLR_VERSION}-complete.jar" -o antlr.jar
if [ ! -d antlr4_rt ]; then
  curl -fsSL "https://www.antlr.org/download/antlr4-cpp-runtime-${ANTLR_VERSION}-source.zip" -o rt.zip
  mkdir antlr4_rt && (cd antlr4_rt && unzip -q ../rt.zip)
fi
[ -f include/json.hpp ] || curl -fsSL "https://raw.githubusercontent.com/nlohmann/json/v3.11.3/single_include/nlohmann/json.hpp" -o include/json.hpp

echo ">> 2. fetch pinned grammar + C++ base classes; port predicates for the C++ target"
for f in Cpp/PlSqlLexerBase.h Cpp/PlSqlParserBase.h Cpp/PlSqlParserBase.cpp; do
  curl -fsSL "$BASE/$f" -o "gen/$(basename "$f")"
done
curl -fsSL "$BASE/PlSqlLexer.g4"  | sed 's/this\./this->/g' > gen/PlSqlLexer.g4
curl -fsSL "$BASE/PlSqlParser.g4" | sed 's/this\./this->/g' > gen/PlSqlParser.g4

echo ">> 3. generate the C++ parser (PlSqlParser.cpp is ~11 MB)"
# Run from inside gen/ with bare filenames: ANTLR resolves the parser's `tokenVocab=PlSqlLexer`
# against the lexer's .tokens in the working dir, which a path-prefixed input arg breaks.
ANTLR_JAR="$PWD/antlr.jar"
(cd gen && java -jar "$ANTLR_JAR" -Dlanguage=Cpp -visitor -o out PlSqlLexer.g4 PlSqlParser.g4)

echo ">> 4. assemble sources + inject the superClass base-header includes ANTLR omits"
# PlSqlParserBase.cpp is already in gen/ (fetched in step 2); only bring in the generated .cpp and
# our extractor. setup.py compiles every gen/*.cpp.
cp gen/out/PlSqlLexer.cpp gen/out/PlSqlParser.cpp ../src/plsql_accel.cpp gen/
cp gen/out/*.h gen/PlSqlLexerBase.h gen/PlSqlParserBase.h include/
sed -i.bak 's|#include "antlr4-runtime.h"|#include "antlr4-runtime.h"\n#include "PlSqlParserBase.h"|' include/PlSqlParser.h
sed -i.bak 's|#include "antlr4-runtime.h"|#include "antlr4-runtime.h"\n#include "PlSqlLexerBase.h"|' include/PlSqlLexer.h
rm -f include/*.bak gen/PlSqlLexer.cpp.rt 2>/dev/null || true

echo ">> 5. compile the extension (module: plsql_accel)"
cp ../setup.py .
python3 setup.py build_ext --inplace
echo ">> done: $(ls plsql_accel*.so 2>/dev/null || echo 'NO .so -- check errors above')"
