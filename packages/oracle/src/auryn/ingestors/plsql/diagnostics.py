"""Diagnostics for a *strict* (fail-fast) SQL / PL-SQL file ingestion run.

The file-based ingestor is best-effort by default (source_analysis.py swallows parse
failures and continues). For a first ingestion of unfamiliar prospect data we instead want the
opposite: stop at the first statement that won't parse and record *everything* needed to
diagnose it offline -- because whoever analyses the report won't have the data.

This module provides the pieces for that mode, all dialect-agnostic:

- `SqlSyntaxError` + `StrictHaltListener`: an ANTLR error listener that raises a positioned
  error on the first lexer/parser error instead of ANTLR's default recover-and-continue.
- `detect_dialect_signals`: scans source for tell-tale tokens of each major SQL dialect, so a
  failure on unknown/mixed input tells us *which* dialect it actually is (the vendored grammar
  is Oracle-only -- a T-SQL/Postgres/MySQL file failing here is expected, and the signals say
  so).
- `source_window` / `enclosing_statement`: show the offending source with a caret and the
  best-effort statement around it.
- `build_failure_report` / `build_encoding_report` / `write_report`: assemble a structured
  record and persist it as both machine-readable JSON and a human-readable text file.

Nothing here imports the ingestor's heavy modules, so it stays cheap to import.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from antlr4.error.ErrorListener import ErrorListener

SCHEMA_VERSION = 1


class SqlSyntaxError(Exception):
    """A single lexer/parser error, carrying its position so the caller can build a report.
    Raised by StrictHaltListener on the first error (ANTLR reports the first mismatch first)."""

    def __init__(self, line: int, column: int, message: str, offending_token: str | None = None, rule: str | None = None):
        self.line = line
        self.column = column
        self.message = message
        self.offending_token = offending_token
        self.rule = rule
        super().__init__(f"line {line}:{column} {message}")

    def __reduce__(self):
        # Picklable across a ProcessPoolExecutor boundary (parallel parsing) -- the default
        # Exception pickling would call __init__ with the wrong args and lose the position.
        return (SqlSyntaxError, (self.line, self.column, self.message, self.offending_token, self.rule))


class StrictHaltError(Exception):
    """Raised to halt the ingestion run *after* a diagnostic report has been written. Carries
    the report and its on-disk path so the CLI can point the user straight at it."""

    def __init__(self, message: str, report: dict, report_path: Path | None):
        self.report = report
        self.report_path = report_path
        super().__init__(message)


class StrictHaltListener(ErrorListener):
    """Raises SqlSyntaxError on the first syntax error (attached to both lexer and parser).
    Contrast source_analysis._RaisingErrorListener, which raises a bare ValueError -- this one
    preserves the offending token and the parser rule in flight for the report."""

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802 (ANTLR API)
        token = getattr(offendingSymbol, "text", None)
        rule = None
        try:
            stack = recognizer.getRuleInvocationStack()  # parser only; lexer lacks it
            rule = stack[0] if stack else None
        except Exception:
            rule = None
        raise SqlSyntaxError(line, column, msg, offending_token=token, rule=rule)


# -- dialect signals -----------------------------------------------------------------------
#
# Each entry is (human label, compiled regex). Deliberately high-signal and conservative:
# a match strongly implies that dialect. Used only for diagnosis, never to change parsing.

def _c(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


_DIALECT_SIGNALS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "oracle": [
        ("VARCHAR2", _c(r"\bVARCHAR2\b")),
        ("NUMBER type", _c(r"\bNUMBER\s*\(")),
        ("CREATE OR REPLACE", _c(r"\bCREATE\s+OR\s+REPLACE\b")),
        ("PACKAGE", _c(r"\bCREATE\s+(OR\s+REPLACE\s+)?PACKAGE\b")),
        ("lone / terminator", _c(r"^\s*/\s*$")),
        ("DUAL", _c(r"\bFROM\s+DUAL\b")),
        ("NVL()", _c(r"\bNVL\s*\(")),
        ("SYSDATE", _c(r"\bSYSDATE\b")),
        ("PLS_INTEGER", _c(r"\bPLS_INTEGER\b")),
        ("EXCEPTION/RAISE", _c(r"\bRAISE_APPLICATION_ERROR\b")),
    ],
    "tsql": [
        ("GO batch separator", _c(r"^\s*GO\s*$")),
        ("[bracket] quoting", _c(r"\[[A-Za-z_][A-Za-z0-9_ ]*\]")),
        ("NVARCHAR", _c(r"\bNVARCHAR\b")),
        ("IDENTITY(", _c(r"\bIDENTITY\s*\(")),
        ("@@ global", _c(r"@@[A-Za-z_]")),
        ("@variable", _c(r"\bDECLARE\s+@")),
        ("GETDATE()", _c(r"\bGETDATE\s*\(\)")),
        ("ISNULL()", _c(r"\bISNULL\s*\(")),
        ("TOP n", _c(r"\bSELECT\s+TOP\s+\d")),
        ("DATETIME2", _c(r"\bDATETIME2\b")),
        ("NVARCHAR(MAX)", _c(r"\bN?VARCHAR\s*\(\s*MAX\s*\)")),
    ],
    "postgres": [
        ("$$ dollar-quote", _c(r"\$\$")),
        ("$tag$ dollar-quote", _c(r"\$[A-Za-z_]\w*\$")),
        ("SERIAL", _c(r"\b(BIG)?SERIAL\b")),
        (":: cast", _c(r"::[A-Za-z_]")),
        ("LANGUAGE plpgsql", _c(r"\bLANGUAGE\s+'?plpgsql'?\b")),
        ("RETURNS TRIGGER", _c(r"\bRETURNS\s+TRIGGER\b")),
        ("JSONB", _c(r"\bJSONB\b")),
        ("GENERATED AS IDENTITY", _c(r"\bGENERATED\s+(ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY\b")),
        ("now()", _c(r"\bNOW\s*\(\)")),
        ("TEXT column", _c(r"\b\w+\s+TEXT\b")),
    ],
    "mysql": [
        ("`backtick` quoting", _c(r"`[^`]+`")),
        ("ENGINE=", _c(r"\bENGINE\s*=")),
        ("AUTO_INCREMENT", _c(r"\bAUTO_INCREMENT\b")),
        ("UNSIGNED", _c(r"\bUNSIGNED\b")),
        ("TINYINT", _c(r"\bTINYINT\b")),
        ("DELIMITER", _c(r"^\s*DELIMITER\b")),
        ("SHOW statement", _c(r"^\s*SHOW\s+")),
        ("charset clause", _c(r"\bDEFAULT\s+CHARSET\b")),
    ],
}


def detect_dialect_signals(source: str) -> dict[str, list[str]]:
    """Which dialect signals appear in `source` -> {dialect: [labels...]} (only non-empty
    dialects). A best-effort fingerprint for unknown/mixed input."""
    out: dict[str, list[str]] = {}
    for dialect, signals in _DIALECT_SIGNALS.items():
        hits = [label for label, pat in signals if pat.search(source)]
        if hits:
            out[dialect] = hits
    return out


def likely_dialect(signals: dict[str, list[str]]) -> str:
    """The dialect with the most distinct signal hits; 'unknown' if none, 'mixed' on a tie
    between two non-trivially-supported dialects."""
    if not signals:
        return "unknown"
    ranked = sorted(signals.items(), key=lambda kv: len(kv[1]), reverse=True)
    if len(ranked) >= 2 and len(ranked[0][1]) == len(ranked[1][1]):
        return "mixed"
    return ranked[0][0]


# -- source rendering ----------------------------------------------------------------------

def source_window(source: str, line: int, column: int, radius: int = 20) -> str:
    """The source around (1-based `line`, 0-based `column`) as numbered lines with a caret
    under the offending column -- always correct, unlike statement splitting."""
    lines = source.splitlines()
    if not lines:
        return ""
    start = max(0, line - 1 - radius)
    end = min(len(lines), line + radius)
    rendered: list[str] = []
    for i in range(start, end):
        marker = ">>" if (i + 1) == line else "  "
        rendered.append(f"{marker} {i + 1:>6} | {lines[i]}")
        if (i + 1) == line:
            rendered.append(f"          | {' ' * max(0, column)}^")
    return "\n".join(rendered)


def _offset_of(source: str, line: int, column: int) -> int:
    lines = source.splitlines(keepends=True)
    if line <= 1:
        return min(column, len(source))
    return min(sum(len(x) for x in lines[: line - 1]) + column, len(source))


def enclosing_statement(source: str, line: int, column: int, max_chars: int = 4000) -> str:
    """Best-effort extraction of the statement containing (line, column): expand out to the
    nearest `;` or lone `/` boundary in each direction, capped. Clearly heuristic -- the caret
    window above is the authoritative view."""
    offset = _offset_of(source, line, column)
    start = 0
    for m in re.finditer(r";|(?:^|\n)\s*/\s*(?:\n|$)", source[:offset]):
        start = m.end()
    tail = source[offset:]
    end = len(source)
    m = re.search(r";|(?:^|\n)\s*/\s*(?:\n|$)", tail)
    if m is not None:
        end = offset + m.end()
    statement = source[start:end].strip()
    if len(statement) > max_chars:
        statement = statement[:max_chars] + "\n... [truncated]"
    return statement


# -- report assembly -----------------------------------------------------------------------

@dataclass
class Progress:
    """What the run accomplished before it halted -- so the report shows how far it got."""

    files_scanned: int = 0
    files_parsed_ok: int = 0
    objects_extracted: dict[str, int] | None = None


def build_failure_report(
    *,
    file_path: str,
    source: str,
    encoding: str,
    error: SqlSyntaxError,
    progress: Progress,
) -> dict:
    signals = detect_dialect_signals(source)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "parse_error",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "grammar": "oracle-plsql (vendored ANTLR4)",
        "file": file_path,
        "encoding": encoding,
        "size_bytes": len(source.encode("utf-8", "replace")),
        "line_count": source.count("\n") + 1,
        "error": {
            "line": error.line,
            "column": error.column,
            "message": error.message,
            "offending_token": error.offending_token,
            "parser_rule": error.rule,
        },
        "source_window": source_window(source, error.line, error.column),
        "enclosing_statement": enclosing_statement(source, error.line, error.column),
        "dialect_signals": signals,
        "likely_dialect": likely_dialect(signals),
        "progress": {
            "files_scanned": progress.files_scanned,
            "files_parsed_ok": progress.files_parsed_ok,
            "objects_extracted": progress.objects_extracted or {},
        },
        "hint": _dialect_hint(likely_dialect(signals)),
    }


def build_encoding_report(
    *,
    file_path: str,
    raw: bytes,
    tried: list[str],
    decode_error: UnicodeDecodeError,
    progress: Progress,
) -> dict:
    bad = decode_error.start
    context = raw[max(0, bad - 24): bad + 24]
    lenient = raw.decode("latin-1", "replace")
    signals = detect_dialect_signals(lenient)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "encoding_error",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file": file_path,
        "size_bytes": len(raw),
        "encodings_tried": tried,
        "first_bad_byte": {
            "offset": bad,
            "byte": f"0x{raw[bad]:02x}" if bad < len(raw) else None,
            "context_hex": context.hex(" "),
            "context_latin1": context.decode("latin-1", "replace"),
        },
        "dialect_signals": signals,
        "likely_dialect": likely_dialect(signals),
        "progress": {
            "files_scanned": progress.files_scanned,
            "files_parsed_ok": progress.files_parsed_ok,
            "objects_extracted": progress.objects_extracted or {},
        },
        "hint": (
            "File is not valid UTF-8. Legacy Oracle exports are often WE8MSWIN1252 (cp1252) or "
            "WE8ISO8859P1 (latin-1). Re-export as UTF-8, or re-run once the encoding is known."
        ),
    }


def _dialect_hint(dialect: str) -> str:
    return {
        "oracle": "Looks like Oracle -- this is a genuine grammar/coverage gap in the vendored "
                  "PL/SQL grammar. Report the enclosing_statement so it can be handled.",
        "tsql": "Looks like SQL Server (T-SQL). The vendored grammar is Oracle-only and cannot "
                "parse T-SQL; a T-SQL engine would be needed.",
        "postgres": "Looks like PostgreSQL. The vendored grammar is Oracle-only and cannot parse "
                    "PL/pgSQL / Postgres DDL; a Postgres engine would be needed.",
        "mysql": "Looks like MySQL/MariaDB. The vendored grammar is Oracle-only; a MySQL engine "
                 "would be needed.",
        "mixed": "Multiple dialects detected. Confirm which engine produced this dump before "
                 "ingesting.",
        "unknown": "No strong dialect signals. Report the enclosing_statement and source_window.",
    }.get(dialect, "")


def write_report(diagnostics_dir: str | Path, report: dict, index: int) -> Path:
    """Persist `report` as both <n>-<basename>.json and .txt under `diagnostics_dir`. Returns
    the JSON path. Directory is created if absent."""
    directory = Path(diagnostics_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{index:03d}-{_slug(report.get('file', 'unknown'))}"
    json_path = directory / f"{stem}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (directory / f"{stem}.txt").write_text(render_text(report), encoding="utf-8")
    return json_path


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(path).name) or "unknown"


def render_text(report: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add(f"SQL INGEST FAILURE ({report.get('kind')})  --  {report.get('generated_at')}")
    add("=" * 78)
    add(f"file:            {report.get('file')}")
    add(f"encoding:        {report.get('encoding', 'n/a')}")
    add(f"size (bytes):    {report.get('size_bytes')}")
    if report.get("kind") == "parse_error":
        err = report["error"]
        add(f"grammar:         {report.get('grammar')}")
        add(f"error:           line {err['line']}:{err['column']}  {err['message']}")
        add(f"offending token: {err.get('offending_token')!r}")
        add(f"parser rule:     {err.get('parser_rule')}")
    elif report.get("kind") == "encoding_error":
        bad = report["first_bad_byte"]
        add(f"encodings tried: {', '.join(report.get('encodings_tried', []))}")
        add(f"first bad byte:  offset {bad['offset']} = {bad['byte']}")
        add(f"context (hex):   {bad['context_hex']}")
    add(f"likely dialect:  {report.get('likely_dialect')}")
    if report.get("dialect_signals"):
        add("dialect signals:")
        for dialect, hits in report["dialect_signals"].items():
            add(f"  - {dialect}: {', '.join(hits)}")
    prog = report.get("progress")
    if prog:
        add(f"progress:        {prog['files_parsed_ok']}/{prog['files_scanned']} files parsed OK before halt")
        if prog.get("objects_extracted"):
            add(f"objects so far:  {prog['objects_extracted']}")
    if report.get("hint"):
        add("")
        add(f"HINT: {report['hint']}")
    if report.get("enclosing_statement"):
        add("")
        add("-- enclosing statement (best-effort) " + "-" * 40)
        add(report["enclosing_statement"])
    if report.get("source_window"):
        add("")
        add("-- source window " + "-" * 60)
        add(report["source_window"])
    add("=" * 78)
    return "\n".join(lines)
