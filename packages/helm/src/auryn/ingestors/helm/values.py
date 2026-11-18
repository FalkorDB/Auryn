"""Resolve a Helm template image reference against a chart's `values.yaml`.

A template names an image like `image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"`; the
concrete repository/tag live in `values.yaml`. We substitute each `{{ .Values.<path> }}` with its
value from values (a dotted lookup) and keep only a FULLY, UNAMBIGUOUSLY resolved ref -- the common
case that lets the chart's workload converge on the shared `Image` hub. Anything we can't resolve to a
single concrete ref is NOT materialized (returns None):
  * an action that isn't a bare `.Values.<path>` -- `.Release.Name`, an `include`, or a pipeline/
    function such as `{{ .Values.tag | upper }}` (we do NOT evaluate pipelines, so taking the raw
    value would be WRONG);
  * a `.Values` path missing from values, or whose value is a map/list or the empty string;
  * a result that still carries a `{{...}}`, a shell `${VAR}`/`$VAR`, whitespace, or an empty
    component (a trailing `:` -> `core.image_ref` would silently force `:latest`, a false edge).
This is render-free and best-effort -- no `helm template`."""

from __future__ import annotations

import re

# A Go-template action `{{ ... }}` / `{{- ... -}}` (whitespace-trim markers included), non-greedy.
_ACTION = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}", re.S)
# A bare `.Values.<dotted.path>` at the START of an action's expression.
_VALUES_REF = re.compile(r"^\.Values\.([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)")


def lookup(values: dict, dotted: str):
    """Walk a dotted path (`image.repository`) into the values tree; None if any step is missing."""
    cur = values
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def resolve_image(raw: str, values: dict) -> str | None:
    """`raw` image string from a template -> a fully concrete ref, or None if it can't be resolved.

    Substitutes every action that is EXACTLY `{{ .Values.<path> }}` with its value from `values`. An
    action with a pipeline/function or any trailing expression (`{{ .Values.tag | upper }}`), a
    non-`.Values` action, a missing/empty/complex value -- all leave the action verbatim, which trips
    the unresolved guard below. A literal image with no actions passes through (still validated)."""
    unresolved = False

    def repl(m: re.Match) -> str:
        nonlocal unresolved
        expr = m.group(1).strip()
        ref = _VALUES_REF.match(expr)
        # Only a BARE `.Values.<path>` (nothing trailing -- no pipeline, no function, no ops) is
        # substitutable; `expr[ref.end():]` must be empty, else we'd wrongly ignore e.g. `| upper`.
        if ref is not None and expr[ref.end():].strip() == "":
            val = lookup(values, ref.group(1))
            if val is not None and not isinstance(val, (dict, list)) and str(val) != "":
                return str(val)
        unresolved = True
        return m.group(0)  # keep the action verbatim so the `{{` check below trips

    out = _ACTION.sub(repl, raw).strip().strip("\"'").strip()
    if unresolved or "{{" in out or "$" in out or not out:
        return None
    if any(c.isspace() for c in out):  # a real image ref has no whitespace (filters prose `image:` keys)
        return None
    if out[0] in ":/@" or out[-1] in ":/@" or "//" in out:  # empty name/tag/digest component
        return None
    return out
