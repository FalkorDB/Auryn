# PL/SQL parse+extract accelerator (optional, compiled)

An **all-C++** accelerator for the file-based PL/SQL ingestor. It parses AND extracts entirely in C++
— the ANTLR C++ runtime over the **same vendored grammar**, plus a C++ port of
`source_analysis.py`'s `_DeclarationVisitor` + `_CallsAndTableRefsVisitor` — and returns only the
small `RawPlsqlFile` result as JSON. The parse tree never crosses into Python, which is what makes it
a real step change (**~12×**) rather than the ~2.6× a tree-translating speedy-antlr build gives.

It builds one CPython extension, `plsql_accel`, exposing `extract(source) -> json`.
`source_analysis.parse_declarations` imports it **opt-in** and falls back to the pure-Python ANTLR
runtime when it's absent — so this is a drop-in speedup, never a hard dependency. Nothing in the base
install changes; you build and install this wheel only where you want the faster parse.

## Correctness

The C++ parser is generated from antlr/grammars-v4 `sql/plsql` at the **pinned commit
`b434a051c56d`**, verified to regenerate the *identical* 1209-rule parser we vendored (same rule/token
tables + predicate set — see `packages/oracle/.../plsql/grammar/__init__.py`). The extractor
(`src/plsql_accel.cpp`) reproduces the Python visitors rule-for-rule. Validation: the full
`tests/ingestors/plsql/` suite (52 tests) passes **with the accelerator active**, and accelerator vs
pure-Python output is asserted **byte-identical** (`RawPlsqlFile` equality) on mixed samples.

The extraction logic is the one place duplicated in C++. If the vendored grammar is regenerated from a
newer commit, bump `GRAMMAR_SHA` in `build.sh`; if a visitor changes, mirror it in
`src/plsql_accel.cpp`. Either kind of drift is caught by the suite (output diverges).

Scope: only `parse_declarations` (the file ingestor's hot path) uses it. The strict/diagnostic path
and `parse_unit` stay on the Python runtime. sqlglot remains the parser for `oraschema` (DDL).

## Performance

Measured **~12× faster** (`parse_declarations`: 23 ms → vs 273 ms pure-Python on a chunky package).
The win is real because the C++ side does parse + extract + returns only a few KB of JSON.

## Build

Local (validation / same-arch):
```bash
pip install setuptools wheel        # + java (for the ANTLR jar), a C++17 compiler
./build.sh                          # -> build/plsql_accel.*.so ; drop on PYTHONPATH or install
```

Deployable **Linux x86-64** wheel (the prospect VM), via manylinux in Docker:
```bash
docker run --rm -v "$PWD":/io quay.io/pypa/manylinux_2_28_x86_64 bash -lc '
  yum install -y java-17-openjdk unzip >/dev/null &&
  export PATH=/opt/python/cp312-cp312/bin:$PATH &&
  pip install setuptools wheel &&
  cd /io && ./build.sh &&
  cd build && python setup.py bdist_wheel &&
  auditwheel repair dist/*.whl -w /io/wheelhouse
'
# -> wheelhouse/auryn_plsql_accel-*-cp312-*-manylinux_2_28_x86_64.whl
```
Install it alongside the ingestor on the VM (`uv pip install wheelhouse/*.whl`); the plsql ingestor
picks it up automatically. Build one wheel per target CPython minor version (cp312, ...).

`build/`, `*.so`, `wheelhouse/` are gitignored — this dir holds only the recipe + `src/plsql_accel.cpp`,
not the ~11 MB of generated C++.
