"""AI naming of detected graph communities.

Given each community's representative member symbols/paths, ask Claude for a
concise human-readable label ("GraphBLAS factory kernels", "Cypher execution
plan"). Kept in its own module so the viewer's core (Flask + FalkorDB) never
hard-depends on the `anthropic` SDK -- it is imported lazily inside the one
function here, and the HTTP layer degrades to the viewer's own heuristic names
when the SDK or an API credential is missing (the import / call simply raises,
and viewer/app.py turns that into a 503).
"""

from __future__ import annotations

import json

# Cheap, deterministic labelling task, but per the Anthropic SDK guidance the
# default model is Opus 4.8 unless the caller explicitly picks another tier.
_MODEL = "claude-opus-4-8"
_MAX_SAMPLES = 20  # representative members per community fed to the model


def label_communities(communities: list[dict]) -> list[str]:
    """`communities` is an ORDERED list of ``{"samples": [str, ...], "size": int}``.
    Returns one label per community, in the same order, so the caller can zip
    them straight back onto its ranked community list. Raises if the `anthropic`
    SDK isn't installed or no API credential resolves (`ANTHROPIC_API_KEY`, an
    `ant` login profile, ...) or the request fails -- the caller handles that by
    keeping its heuristic labels."""
    import anthropic  # lazy: optional dependency, only needed for this feature

    client = anthropic.Anthropic()  # resolves the credential from the environment

    listing = "\n".join(
        f"{i}. (size {c.get('size', '?')}) members: "
        + ", ".join(str(s) for s in (c.get("samples") or [])[:_MAX_SAMPLES])
        for i, c in enumerate(communities)
    )
    n = len(communities)
    prompt = (
        "Below are clusters (communities) that label-propagation found in a CODE "
        "dependency graph. Each cluster lists representative member symbols and file "
        "paths. Give each cluster a concise, human-readable label of 2-4 words that "
        "names the subsystem or theme it represents -- e.g. 'GraphBLAS factory "
        "kernels', 'Cypher execution plan', 'QuickJS engine'. Prefer the specific "
        "subsystem over a generic word, and make the labels distinct from one "
        f"another. Return exactly {n} labels, in the same order as the clusters.\n\n"
        f"{listing}"
    )
    schema = {
        "type": "object",
        "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
        "required": ["labels"],
        "additionalProperties": False,
    }
    message = client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), "")
    labels = [str(x) for x in (json.loads(text) or {}).get("labels", [])][:n]
    labels += [""] * (n - len(labels))  # pad so the caller can zip safely
    return labels
