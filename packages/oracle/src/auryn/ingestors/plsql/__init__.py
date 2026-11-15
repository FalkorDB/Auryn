"""The PL/SQL language family. Despite the package name it exposes THREE registrations,
now split by concern so file-based discovery stays import-light:

- the `plsql` GitLanguage (git_sync.py) -- registered via the import-light `plugin` module
  (the `auryn.languages` entry point), so discovery never imports the ANTLR grammar or
  oracledb; the engine loads only when a source contains PL/SQL files.
- `oracle` (ingestor.py, PLSQLIngestor): reads a live Oracle connection's catalog.
- `plsql` (file_ingestor.py, PLSQLFileIngestor): parses PL/SQL *source files*.

The two live-source BaseIngestors register via the `auryn.ingestors` group (loaded only
by `auryn run`). The shared parser (grammar/, source_analysis.py) and schema (schema.py)
live here because they belong to the language.
"""
