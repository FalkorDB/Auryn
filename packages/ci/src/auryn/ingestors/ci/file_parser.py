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


def _first_non_flag(args: str) -> str:
    """The first argument that isn't a `-flag` -- the image in `docker push [--flag ...] <image>`."""
    for tok in args.split():
        if not tok.startswith("-"):
            return tok
    return ""


def _images_from_step(step: dict) -> list[str]:
    out: list[str] = []
    uses = _scalar(step.get("uses"))
    if "docker/build-push-action" in uses:
        with_ = step.get("with")
        if isinstance(with_, dict):
            out += _split_tags(with_.get("tags"))
    run = _scalar(step.get("run"))
    if run:
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
