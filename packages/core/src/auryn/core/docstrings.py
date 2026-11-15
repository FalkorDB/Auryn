"""Capture the documentation comment attached to a declaration, uniformly across engines.

Two extraction entry points feed one cleaner:

- `leading_comment(node, source)` for tree-sitter engines: a declaration's doc comment is
  the run of comment nodes immediately preceding it (its previous *named* siblings) -- a
  language-agnostic fact of every C-family / Go / Rust / ... grammar. tree-sitter attaches
  such comments as siblings of the declaration, never as children, so they never fall inside
  the node's own byte span (its `source` property); this walks back to them.
- `leading_comment_from_source(text, top_line)` for AST engines (Python's `ast`, C#'s
  Roslyn model) whose parser discards comments: re-reads the source text directly above the
  declaration's first line.

Both return text run through `clean_comment`, which strips the comment markers so the stored
`doc` is the prose a human wrote -- the cleanest input to an embedding, not marker noise.

`doc` is a shared node property (see docs/graph-model.md): ingestors store the leading comment
of each Function/Method/Class/Struct/Interface/... here. It is the natural-language half of the
`doc + source` document the embedding layer embeds.
"""

from __future__ import annotations

# Line-comment markers, longest-first so `///` wins over `//`. Covers every language this
# project ingests: C-family/Go/Rust/TS `//` (+ Rust/C# doc `///`, Rust inner `//!`),
# Python/PHP/Ruby `#`, SQL/PL-SQL `--`.
_LINE_MARKERS = ("///", "//!", "//", "#", "--")


def clean_comment(raw: str) -> str:
    """Strip comment markers from a raw comment span, returning the prose within.

    Handles the line-comment tokens in `_LINE_MARKERS` and block comments (`/* ... */`, with
    per-line leading `*` continuation). Runs of blank lines are collapsed and leading/trailing
    blanks dropped. Marker matching is anchored to each (whitespace-stripped) line start, so an
    in-prose `--` or `*` is never touched."""
    text = raw.strip()
    if text.startswith("/*"):
        text = text[2:]
        if text.endswith("*/"):
            text = text[:-2]
    stripped_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        for marker in _LINE_MARKERS:
            if s.startswith(marker):
                s = s[len(marker):].lstrip()
                break
        else:
            if s.endswith("*/"):
                s = s[:-2].rstrip()
            if s.startswith("*") and not s.startswith("*/"):
                s = s[1:].lstrip()
        stripped_lines.append(s)

    # Collapse consecutive blanks, then trim blank ends.
    collapsed: list[str] = []
    for s in stripped_lines:
        if s or (collapsed and collapsed[-1]):
            collapsed.append(s)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    return "\n".join(collapsed)


def leading_comment(node, source: bytes) -> str:
    """The documentation comment immediately preceding a tree-sitter `node`, cleaned, or "".

    Walks `node`'s previous named siblings while each is a comment node (its `type` contains
    "comment" -- covers grammars that name them `comment`, `line_comment`, or `block_comment`)
    and is vertically adjacent to what follows it. A blank line between the comment and the
    declaration means it is not a doc comment, so the walk stops there. Stacked `//` line
    comments (each its own node in most grammars) are collected together as one block, in
    source order.

    Newline-robust: some grammars (e.g. tree-sitter-rust) include a line comment's trailing
    newline in its byte span, which pushes `end_point` onto the next row. Trusting `end_point`
    then both (a) mis-measures the blank-line gap (off-by-one -> a detached comment wrongly
    attaches) and (b) leaves a stray blank between stacked comments after the join. So each
    span is `rstrip`ped of trailing newlines and adjacency is measured from the comment's real
    last *content* row (derived from its text), not `end_point`. This is a no-op for grammars
    whose comment spans exclude the newline (Go, C, Java, ...)."""
    texts: list[str] = []
    cur_start_row = node.start_point[0]
    cur = node
    while True:
        prev = cur.prev_named_sibling
        if prev is None or "comment" not in prev.type:
            break
        text = source[prev.start_byte : prev.end_byte].decode("utf-8", "replace").rstrip("\n")
        content_end_row = prev.start_point[0] + text.count("\n")
        if cur_start_row - content_end_row > 1:  # blank line between -> not attached
            break
        texts.append(text)
        cur = prev
        cur_start_row = prev.start_point[0]
    if not texts:
        return ""
    texts.reverse()
    return clean_comment("\n".join(texts))


def leading_comment_from_source(
    text: str, top_line: int, prefixes: tuple[str, ...] = ("#",), lines: list[str] | None = None
) -> str:
    """The line-comment block directly above line `top_line` (1-based) in `text`, cleaned, or "".

    For AST engines whose parser drops comments: Python (`prefixes=("#",)`, the default) and
    C#'s `///` XML docs (`prefixes=("///", "//")`). Walks upward from the line just above
    `top_line`, collecting contiguous lines whose first non-space characters match a prefix;
    stops at the first blank or non-comment line, so only an adjacent block attaches -- matching
    the tree-sitter rule. `top_line` should be the declaration's *first* line, i.e. above any
    decorators/attributes (a leading comment sits above those).

    `lines` lets a caller pass `text.splitlines()` computed once and reuse it across many calls on
    the same file -- splitting per call is O(file), which is quadratic over a file's declarations
    (a pathological cost on large generated modules). When omitted it's split here, unchanged."""
    if lines is None:
        lines = text.splitlines()
    collected: list[str] = []
    i = top_line - 2  # 0-based index of the line directly above the declaration
    while i >= 0:
        stripped = lines[i].strip()
        if stripped and any(stripped.startswith(p) for p in prefixes):
            collected.append(lines[i])
            i -= 1
        else:
            break
    if not collected:
        return ""
    collected.reverse()
    return clean_comment("\n".join(collected))
