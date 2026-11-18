"""Hand-rolled Dockerfile parser -> `ParsedDockerfile` (no native dependency).

Dockerfile syntax is small enough that a line tokenizer is faithful and keeps the plugin
dependency-free and its tests hermetic; the two genuinely fiddly bits -- line continuations (`\\`)
and whole-line comments -- are handled explicitly below. ADR 0007 §7 notes a `tree-sitter-dockerfile`
engine can replace this behind the same `parse_source` seam (for heredocs / parser directives /
alternate `escape`) without any change to `build.py`.

Scope (v1): the instructions the deployment graph needs -- FROM (+ `AS`, multi-stage bases),
COPY/ADD (+ `--from`), EXPOSE, ENTRYPOINT, CMD, WORKDIR, and pre-FROM ARG. Other instructions (RUN,
ENV, LABEL, USER, ...) are recognized and skipped; ENV->ConfigValue is a later arm (ADR 0007)."""

from __future__ import annotations

import json

from auryn.ingestors.docker.model import Copy, ParsedDockerfile, Stage

_CONTINUATION = "\\"


def _logical_lines(text: str) -> list[str]:
    """Collapse the physical lines into logical instructions: drop whole-line comments and blank
    lines, then join any line ending in a backslash with the next (Dockerfile line continuation)."""
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank line or whole-line comment / parser directive -- dropped even mid-continuation
        line = raw.rstrip()
        if line.endswith(_CONTINUATION):
            buf += line[: -len(_CONTINUATION)] + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _split_instruction(line: str) -> tuple[str, str]:
    """`COPY --from=build /a /b` -> ("COPY", "--from=build /a /b"). Instruction keyword is
    case-insensitive; splits on the first whitespace RUN so a tab after the keyword (`FROM\\talpine`)
    is accepted, not silently dropped."""
    parts = line.split(None, 1)
    return parts[0].upper(), (parts[1].strip() if len(parts) > 1 else "")


def _tokens(rest: str) -> list[str]:
    return rest.split()


def _parse_from(rest: str) -> tuple[str, str | None]:
    """`[--platform=...] <image|stage> [AS <name>]` -> (base, stage_name)."""
    toks = [t for t in _tokens(rest) if not t.startswith("--")]
    if not toks:
        return "", None
    base = toks[0]
    name = None
    for i, t in enumerate(toks):
        if t.lower() == "as" and i + 1 < len(toks):
            name = toks[i + 1]
            break
    return base, name


def _parse_copy(rest: str) -> Copy | None:
    """`[--from=X] [--chown=...] src... dest`. Leading `--` flags are stripped FIRST, so both forms
    accept them -- the shell form (whitespace-separated) and the JSON exec form (`["src", "dest"]`,
    which Docker also allows flags before, e.g. `COPY --chown=x ["a", "b"]`)."""
    toks = _tokens(rest)
    from_stage = None
    i = 0
    while i < len(toks) and toks[i].startswith("--"):
        if toks[i].startswith("--from="):
            from_stage = toks[i].split("=", 1)[1]
        i += 1
    operands = toks[i:]
    if not operands:
        return None

    if operands[0].startswith("["):  # JSON exec form, now that any flags are removed
        try:
            arr = json.loads(" ".join(operands))
        except ValueError:
            return None
        if isinstance(arr, list) and len(arr) >= 2:
            return Copy(sources=tuple(str(x) for x in arr[:-1]), dest=str(arr[-1]), from_stage=from_stage)
        return None

    if len(operands) < 2:
        return None
    return Copy(sources=tuple(operands[:-1]), dest=operands[-1], from_stage=from_stage)


def parse_source(data: str | bytes) -> ParsedDockerfile:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")

    args: dict[str, str] = {}
    stages: list[Stage] = []

    # Mutable accumulators for the stage currently being built (flushed on the next FROM / at EOF).
    cur: dict | None = None

    def flush() -> None:
        if cur is None:
            return
        stages.append(
            Stage(
                index=cur["index"],
                base=cur["base"],
                name=cur["name"],
                copies=tuple(cur["copies"]),
                exposed_ports=tuple(cur["ports"]),
                entrypoint=cur["entrypoint"],
                cmd=cur["cmd"],
                workdir=cur["workdir"],
            )
        )

    for line in _logical_lines(data):
        instr, rest = _split_instruction(line)
        if instr == "FROM":
            flush()
            base, name = _parse_from(rest)
            cur = {
                "index": len(stages),
                "base": base,
                "name": name,
                "copies": [],
                "ports": [],
                "entrypoint": None,
                "cmd": None,
                "workdir": None,
            }
        elif cur is None:
            # Pre-FROM preamble: only global ARGs matter (they can parameterize a `FROM ${ARG}`).
            if instr == "ARG" and rest:
                k, _, v = rest.partition("=")
                args[k.strip()] = v.strip()
        elif instr in ("COPY", "ADD"):
            c = _parse_copy(rest)
            if c is not None:
                cur["copies"].append(c)
        elif instr == "EXPOSE":
            cur["ports"].extend(_tokens(rest))
        elif instr == "ENTRYPOINT":
            cur["entrypoint"] = rest
        elif instr == "CMD":
            cur["cmd"] = rest
        elif instr == "WORKDIR":
            cur["workdir"] = rest.strip()
        # every other instruction (RUN, ENV, LABEL, USER, ...) is intentionally ignored in v1

    flush()
    return ParsedDockerfile(stages=tuple(stages), args=args)
