"""Resolve a Helm template image reference against a chart's `values.yaml`.

A template names an image like `image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"`; the
concrete repository/tag live in `values.yaml`. We substitute each `{{ .Values.<path> }}` with its
default from values (a dotted lookup) and keep only a FULLY resolved ref -- the common case that lets
the chart's workload converge on the shared `Image` hub. A ref that still carries a `{{...}}` after
substitution (it referenced `.Release.Name`, an `include`, a pipeline we don't evaluate, or a missing
value) is NOT materialized: `core.image_ref` keys/flags on `${VAR}`, not Helm's `{{}}`, and a partially
templated ref can't be keyed meaningfully. This is render-free and best-effort -- no `helm template`."""

from __future__ import annotations

import re

# A Go-template action `{{ ... }}` / `{{- ... -}}` (whitespace-trim markers included), non-greedy.
_ACTION = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}", re.S)
# A bare `.Values.<dotted.path>` at the START of an action's expression (ignoring a trailing
# `| default "x"` pipeline etc. -- we take the declared default from values, not the pipe fallback).
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

    Substitutes every `{{ .Values.<path> }}` with its value from `values`; any other action (or a
    `.Values` path absent from values) leaves a `{{...}}` behind, which makes the ref unresolvable ->
    None. A literal image with no actions passes through unchanged."""
    unresolved = False

    def repl(m: re.Match) -> str:
        nonlocal unresolved
        ref = _VALUES_REF.match(m.group(1).strip())
        if ref is not None:
            val = lookup(values, ref.group(1))
            if val is not None and not isinstance(val, (dict, list)):
                return str(val)
        unresolved = True
        return m.group(0)  # keep the action verbatim so the `{{` check below trips

    out = _ACTION.sub(repl, raw).strip().strip("\"'").strip()
    if unresolved or "{{" in out or not out:
        return None
    return out
