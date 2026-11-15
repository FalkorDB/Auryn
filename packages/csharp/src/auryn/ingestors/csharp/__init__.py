"""The C# language family. Registration is import-light: the `auryn.languages` entry
point targets the tiny `plugin` module (suffixes + a lazy GitLanguage), so file-based
discovery never imports tree-sitter or the Roslyn extractor. The live-source CSharpIngestor
registers separately via the `auryn.ingestors` group (loaded only by `auryn run`).
"""
