"""The Python language family. Registration is import-light: the `auryn.languages`
entry point targets the tiny `plugin` module (suffixes + a lazy GitLanguage), so discovery
never imports the `ast`/Jedi extraction engine in git_sync -- that happens only when a
source actually contains `.py` files. See plugin.py.
"""
