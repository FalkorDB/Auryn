"""Builds the `plsql_accel` CPython extension: the ANTLR4 C++ runtime + the generated C++ PL/SQL
lexer/parser + the hand-written C++ extractor (src/plsql_accel.cpp), linked into one module. Run via
build.sh (which generates the C++ parser + fetches json.hpp into ./build/ first). Raw CPython C-API,
no pybind11."""

import glob
import sysconfig

from setuptools import Extension, setup

# Only the generated lexer/parser/base + our extractor -- NOT any speedy-antlr translator (this build
# doesn't translate the tree to Python; it extracts in C++).
generated = ["gen/PlSqlLexer.cpp", "gen/PlSqlParser.cpp", "gen/PlSqlParserBase.cpp", "gen/plsql_accel.cpp"]
antlr_runtime = glob.glob("antlr4_rt/runtime/src/**/*.cpp", recursive=True)

setup(
    name="auryn-plsql-accel",
    version="0.1.0",
    description="Optional all-C++ ANTLR parse+extract accelerator for the PL/SQL ingestor "
    "(drop-in; byte-identical to the vendored-grammar Python extraction).",
    ext_modules=[
        Extension(
            "plsql_accel",
            sources=sorted(antlr_runtime) + generated,
            include_dirs=["antlr4_rt/runtime/src", "include", sysconfig.get_path("include")],
            # -O1: the ~11 MB generated PlSqlParser.cpp is mostly the serialized ATN; higher opt costs
            # far more compile time/memory for little runtime gain.
            extra_compile_args=["-std=c++17", "-O1", "-DANTLR4CPP_STATIC", "-fvisibility=hidden"],
            language="c++",
        )
    ],
)
