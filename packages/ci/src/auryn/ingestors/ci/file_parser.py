"""Parse a GitHub Actions workflow (`.github/workflows/*.yml`) -> `ParsedWorkflow`. Pure, no graph.

Extracts the images a workflow BUILDS or PUSHES -- the hub seam -- from its steps:
  * a `docker/build-push-action` step's `with.tags`;
  * a `run:` step's `docker build -t <tag>` / `docker buildx build --tag <tag>` / `docker push <tag>`.
A fully-templated tag (`${{ ... }}`) can't be keyed on the concrete hub, so only literal refs are kept.
Best-effort and never fatal: a file without a `jobs:` map (or unreadable) yields an invalid workflow."""

from __future__ import annotations

import re

import yaml

from auryn.ingestors.ci.model import ParsedWorkflow

# A `docker build`/`buildx build` command (matched per-command, so a stray `-t` in another command in
# the same `run:` block is ignored).
_HAS_BUILD = re.compile(r"docker\s+(?:buildx\s+)?build\b", re.I)
_BUILD_TAG = re.compile(r"(?:^|\s)(?:-t|--tag)[=\s]+(\S+)")
_PUSH = re.compile(r"docker\s+push\s+(.+)", re.I)
# Characters that can't appear in a LITERAL image ref: a `${{ }}`/shell variable, a `$(...)`/backtick
# command substitution, or a shell metacharacter. A tag carrying one is dynamic -> not keyed on the hub.
_IMAGE_FORBIDDEN = frozenset("${}()<>|&;`\"' \t")


def _scalar(v) -> str:
    return str(v).strip() if isinstance(v, (str, int, float)) else ""


def _safe_yaml(data: str | bytes) -> dict:
    try:
        doc = yaml.safe_load(data)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def _plausible_image(ref: str) -> bool:
    """A literal image ref: non-empty, no templated `${{...}}`/shell metacharacter, and -- if the last
    path segment carries a `:tag` -- a non-empty repository and tag."""
    if not ref or "${{" in ref or any(c in _IMAGE_FORBIDDEN for c in ref):
        return False
    last = ref.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":" in last:
        repo_tail, _, tag = last.rpartition(":")
        if not repo_tail or not tag:
            return False
    return True


def _split_tags(tags) -> list[str]:
    """`with.tags` -> a list of refs. It is a YAML list, or a string with newline/comma separators."""
    if isinstance(tags, list):
        items = [_scalar(t) for t in tags]
    elif isinstance(tags, str):
        items = re.split(r"[\n,]", tags)
    else:
        return []
    return [t.strip().strip("\"'") for t in items if t.strip()]


# `docker push` options that consume the FOLLOWING token as their value (`--platform linux/amd64`),
# so that value isn't mistaken for the positional image. The `--flag=value` form needs no entry (it's
# a single `-`-prefixed token). `-a`/`--all-tags`/`-q`/`--quiet`/`--disable-content-trust` are booleans.
_PUSH_VALUE_FLAGS = frozenset({"--platform"})


def _first_non_flag(args: str) -> str:
    """The first argument that is neither a `-flag` nor a flag's consumed value -- the image in
    `docker push [--flag [value]] <image>`."""
    toks = args.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok in _PUSH_VALUE_FLAGS:  # skip the flag AND its value (`--platform linux/amd64`)
            i += 2
            continue
        if tok.startswith("-"):  # a boolean flag or a `--flag=value` form
            i += 1
            continue
        return tok
    return ""


def _strip_comments(text: str) -> str:
    """Drop shell comments from a (possibly multi-line) `run:` block: a `#` that begins a word (line
    start or after whitespace) and is NOT inside single/double quotes starts a comment running to the
    end of its physical line. A `#` mid-token (`tag#sha`) or quoted (`echo "a # b"`) is literal.

    Single-pass so it is faithful to shell lexing across lines: quote state is tracked ACROSS physical
    lines (a `"..."` string may span newlines), and a backslash escapes the next character except inside
    single quotes (so `echo \\"` doesn't open a quote, and the following `#` IS a comment). A comment is
    never continued by a trailing backslash, so this runs BEFORE joining `\\`-continuations."""
    out: list[str] = []
    quote = ""      # "", "'", or '"' -- the open quote, if any
    prev_ws = True  # at a word boundary (line start / after whitespace)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < n:  # backslash escapes next char (not inside '...')
            out.append(ch)
            out.append(text[i + 1])
            # A `\<newline>` continuation is removed by the shell, so the word boundary from BEFORE the
            # backslash carries to the joined text (`echo x \<NL># c` -> `# c` is still a comment); any
            # other escaped char is a literal word char.
            if text[i + 1] != "\n":
                prev_ws = False
            i += 2
            continue
        if quote:  # inside a quote: only the matching quote closes it; # is literal
            out.append(ch)
            if ch == quote:
                quote = ""
            prev_ws = False
        elif ch in "'\"":
            quote = ch
            out.append(ch)
            prev_ws = False
        elif ch == "#" and prev_ws:  # a real comment -> skip to end of THIS physical line
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
            prev_ws = ch in " \t\n;&|()"  # whitespace, a newline, OR an unquoted shell operator (`true;# c`)
        i += 1
    return "".join(out)


def _images_from_step(step: dict) -> list[str]:
    out: list[str] = []
    uses = _scalar(step.get("uses"))
    if "docker/build-push-action" in uses:
        with_ = step.get("with")
        if isinstance(with_, dict):
            out += _split_tags(with_.get("tags"))
    run = _scalar(step.get("run"))
    if run:
        # Strip shell comments first (a comment isn't continued by a trailing `\`), THEN join
        # `\`-continuations -- so a `# docker push x \` comment can't swallow the next line, and a real
        # `docker build -t x \` <newline> `.` stays one command. `_strip_comments` is quote/escape aware.
        run = _strip_comments(run)
        run = run.replace("\\\n", " ")
        # per COMMAND (split on shell separators), so a `-t` or `push` only counts inside its own
        # `docker build`/`docker push` -- not a `-t` from an unrelated command in the same block.
        for cmd in re.split(r"&&|\|\||;|\n", run):
            if _HAS_BUILD.search(cmd):
                out += [m.group(1) for m in _BUILD_TAG.finditer(cmd)]
            pm = _PUSH.search(cmd)
            if pm:
                img = _first_non_flag(pm.group(1))  # skip `docker push --quiet <image>` flags
                if img:
                    out.append(img)
    return [t.strip().strip("\"'") for t in out if t.strip()]


def parse_workflow(data: str | bytes) -> ParsedWorkflow:
    doc = _safe_yaml(data)
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return ParsedWorkflow(valid=False)  # not a usable GitHub Actions workflow
    images: dict[str, None] = {}
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            for ref in _images_from_step(step):
                if _plausible_image(ref):
                    images.setdefault(ref, None)
    return ParsedWorkflow(valid=True, name=_scalar(doc.get("name")), built_images=tuple(images))
