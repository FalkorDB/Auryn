"""The C language family. Registration is import-light: the `auryn.languages` entry
point targets the tiny `plugin` module (suffixes + a lazy GitLanguage), so file-based
discovery never imports tree-sitter. Fast engine = tree-sitter-c (no build config); a deep
libclang engine (resolved types, header<->.c linking, cross-TU calls) is the planned second
phase and slots in as the GitLanguage's `deep_sync`.
"""
