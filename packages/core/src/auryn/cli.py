"""Command-line entrypoint. Onboarding: `auryn install` / `uninstall` (register the MCP
server + skill with detected agents; choose local-lite vs remote FalkorDB) and `serve` / `stop`
(the lazy local FalkorDB daemon). Core: `auryn run <name>`, `view`, `mcp`,
`git-sync <name>`, `ingest <dir|repo>`, and `quickstart <dir>` (zero-setup embedded ingest+view).

For the local backend, `view`/`mcp`/`ingest` resolve their connection via ensure_server(), which
starts the bundled FalkorDB on demand; the graph defaults to one per repository (CLAUDE_PROJECT_DIR
for the agent-spawned MCP server, else the path/cwd).

Adding a new ingestor never requires touching this file: `run` dynamically attaches
a subcommand for every ingestor discovered by discover_ingestors() that implements
build_cli_command() (see BaseIngestor.build_cli_command / CONTRIBUTING.md).
`git-sync` is wired directly here instead, since it isn't a BaseIngestor (its
lifecycle -- a persistent repo cache, a commit marker, real deletions -- doesn't
fit BaseIngestor.run()'s stateless contract; see ingestors/csharp/git_sync.py).
`ingest` drives the multi-language endpoint (core/ingest.py) over a whole source at
once -- a git repo (`ingest repo`) or a local directory tree (`ingest dir`) -- and is
the CLI twin of the MCP `ingest_repository`/`ingest_directory` tools.
"""

from __future__ import annotations

import io
import sys

import click

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect, connect_client
from auryn.core.logging_config import setup_logging
from auryn.core.registry import discover_ingestors

# A character the CLI prints in several messages (install summaries, progress output).
_PROBE_CHAR = "\u2192"  # →


def _ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 when the active codec cannot represent the
    Unicode the CLI prints. On Windows, both console streams and redirects default to a
    locale codec (cp1252 in Western locales), which raises UnicodeEncodeError on arrows,
    dots and box characters instead of writing them. Leave every other stream alone: a
    wrapper that already speaks UTF-8 (the MCP server's JSON-RPC stdio) and capture
    buffers used by tests must not be rewrapped."""
    for stream in (sys.stdout, sys.stderr):
        if not isinstance(stream, io.TextIOWrapper):
            continue
        try:
            _PROBE_CHAR.encode(stream.encoding or "ascii")
        except (LookupError, UnicodeEncodeError):
            stream.reconfigure(encoding="utf-8", errors="replace")


@click.group()
@click.option("-v", "--verbose", is_flag=True, default=False,
              help="Log every file as it's parsed (a live progress line per file). Off by default; "
                   "also settable with AURYN_VERBOSE=1. On the pooled C#/PL/SQL parsers, verbose "
                   "also parses sequentially so a crash pinpoints the offending file.")
def cli(verbose: bool) -> None:
    """Extracts structural metadata from various sources into a FalkorDB graph."""
    from auryn.core import progress

    _ensure_utf8_stdio()
    setup_logging()
    # --verbose only ever forces per-file logging ON; absence leaves AURYN_VERBOSE as-is, so an
    # externally-set env var still enables it (e.g. for the MCP server or a wrapper script).
    if verbose:
        progress.set_verbose(True)


@click.group()
@click.option("--host", default="localhost", show_default=True, help="FalkorDB host.")
@click.option("--port", default=6379, show_default=True, type=int, help="FalkorDB port.")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.option("--graph-name", default="auryn", show_default=True, help="Target graph name.")
@click.pass_context
def run(
    ctx: click.Context,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    ssl: bool,
    graph_name: str,
) -> None:
    """Run a registered ingestor against a FalkorDB instance."""
    # Deliberately just stashes the config, without connecting -- connecting here
    # would run even for `auryn run <name> --help`, since click invokes a group's
    # callback before dispatching to its subcommand.
    ctx.obj = {
        "falkordb_config": FalkorDBConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            ssl=ssl,
            graph_name=graph_name,
        )
    }


def _attach_ingestor_commands() -> None:
    for ingestor_cls in discover_ingestors().values():
        try:
            command = ingestor_cls.build_cli_command()
        except NotImplementedError:
            continue
        run.add_command(command)


def _resolve_connection(
    host: str | None, port: int | None, username: str | None, password: str | None, ssl: bool
) -> FalkorDBConfig:
    """A ready-to-connect FalkorDBConfig. An explicit ``--host`` means "connect directly there";
    otherwise resolve the configured backend and, for the local one, lazily start its daemon.
    So a fresh machine with no config still works (defaults to a local, on-demand FalkorDB)."""
    if host:
        return FalkorDBConfig(host=host, port=port or 6379, username=username, password=password, ssl=ssl)
    from auryn.core.app_config import load_backend
    from auryn.core.server_manager import ensure_server

    return ensure_server(load_backend())


def _purge_git_info_backend() -> int:
    """Remove Git-information nodes from every graph on the configured backend."""
    from auryn.core.git_info import purge_git_info

    client = connect_client(_resolve_connection(None, None, None, None, False))
    return sum(purge_git_info(GraphWriter(client.select_graph(name))) for name in client.list_graphs())


@cli.command("view")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host the viewer's web server binds to.")
@click.option("--port", default=8080, show_default=True, type=int, help="Port the viewer's web server binds to.")
@click.option("--falkordb-host", default=None, help="FalkorDB host. Default: the configured backend (local starts on demand).")
@click.option("--falkordb-port", default=None, type=int, help="FalkorDB port (only with --falkordb-host).")
@click.option("--falkordb-username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--falkordb-password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--falkordb-ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
def view(
    host: str,
    port: int,
    falkordb_host: str | None,
    falkordb_port: int | None,
    falkordb_username: str | None,
    falkordb_password: str | None,
    falkordb_ssl: bool,
) -> None:
    """Serve a web UI for browsing every graph on a FalkorDB instance."""
    from auryn.viewer.app import create_app

    config = _resolve_connection(falkordb_host, falkordb_port, falkordb_username, falkordb_password, falkordb_ssl)
    app = create_app(connect_client(config), host)
    click.echo(f"Serving graph viewer on http://{host}:{port}")
    app.run(host=host, port=port)


@cli.command("mcp")
@click.option("--graph-name", default=None, help="Graph to expose. Default: one per repo, from CLAUDE_PROJECT_DIR/cwd.")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend (local starts on demand).")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
def mcp_serve(
    graph_name: str | None,
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
    ssl: bool,
) -> None:
    """Serve an MCP server exposing curated tools over one graph (stdio transport).

    This is what Claude Code launches after `auryn install`; the graph defaults to one per
    repository (derived from CLAUDE_PROJECT_DIR), and for the local backend the FalkorDB daemon
    is started on demand. See auryn/mcp/server.py for the tool set."""
    from auryn.core.project import default_graph_name
    from auryn.mcp.server import create_server

    config = _resolve_connection(host, port, username, password, ssl)
    # An explicit --host is an ad-hoc connection the background embed daemon (which reconnects via
    # the configured backend) might not match, so force inline embedding for this server's ingests.
    server = create_server(connect_client(config), graph_name or default_graph_name(),
                           embed=("inline" if host else None))
    server.run("stdio")


@cli.command("embed")
@click.option("--graph-name", default=None, help="Graph to embed. Default: one per repo, from CLAUDE_PROJECT_DIR/cwd.")
@click.option("--model", default=None, help="Embedding model id. Default: the local fastembed code model.")
@click.option("--reembed", is_flag=True, default=False,
              help="Drop the vector index and re-embed the whole graph from scratch (use when switching models).")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend (local starts on demand).")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
def embed(
    graph_name: str | None,
    model: str | None,
    reembed: bool,
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
    ssl: bool,
) -> None:
    """Compute vector embeddings for a graph's callables/types/files now, then build the vector
    index -- what powers the MCP `search_code` (semantic) tool. Embeddings are built automatically
    in the background on every ingest, so this is only for forcing a build on demand (e.g. after an
    offline ingest, or `--reembed` to rebuild the whole graph after a `--model` change -- the index
    is fixed-dimension). Idempotent: only nodes without an embedding are processed. The local
    embedding model is auto-installed on first use (opt-out; AURYN_AUTO_INSTALL=0). See
    auryn/core/embeddings/."""
    from auryn.core.embeddings import configured_model, get_embedder
    from auryn.core.embeddings.worker import embed_graph, reembed_all
    from auryn.core.project import default_graph_name

    config = _resolve_connection(host, port, username, password, ssl)
    graph = connect_client(config).select_graph(graph_name or default_graph_name())
    run = reembed_all if reembed else embed_graph
    # Fall back to the AURYN_EMBED_MODEL override (what the auto-embed path honors) when --model is
    # not given, so the CLI can pin a smaller model than the default on memory-constrained machines.
    stats = run(GraphWriter(graph), get_embedder(model or configured_model()))
    # `index_ready` is False both when nothing was embedded (the index wasn't touched) and when the
    # build couldn't be confirmed (it outlasted the wait, or its state can't be read -- see
    # GraphWriter.await_indexes). `--reembed` DROPS the index up front, so "unchanged" would be wrong
    # even with zero embeds -- the index is gone until a later run rebuilds it.
    if reembed and not stats.embedded:
        index = "reset -- dropped, and no nodes to rebuild it from"
    elif not stats.embedded:
        index = "unchanged"
    elif stats.index_ready:
        index = "ready"
    else:
        index = "not ready yet -- searches may return nothing until it is"
    click.echo(
        f"Embedded {stats.embedded} node(s) ({stats.scanned} scanned) with {stats.model_id} "
        f"(dim={stats.dim}); vector index {index}."
    )


@cli.command("embed-daemon", hidden=True)
def embed_daemon() -> None:
    """Internal: the background embedding worker. Normally spawned detached by an ingest whose
    embed mode is `background` (see embeddings.daemon); it drains the request queue, embedding each
    graph's pending vectors, and self-terminates once the queue goes idle. Run directly to debug."""
    from auryn.core.embeddings.daemon import run_embed_daemon

    run_embed_daemon()


@cli.command("deepen-daemon", hidden=True)
def deepen_daemon() -> None:
    """Internal: the background deep-analysis worker. Normally spawned detached by an ingest whose
    deep mode is `async` (see deepen.daemon); it drains the request queue, re-running each repo's
    ingest with deep=inline (the fast phase short-circuits), and self-terminates once the queue goes
    idle. Run directly to debug."""
    from auryn.core.deepen.daemon import run_deepen_daemon

    run_deepen_daemon()


def _render_impact(report: dict) -> None:
    """Human-readable rendering of an impact_of_diff report."""
    syms = report.get("changed_symbols", [])
    if not syms:
        click.echo("No graph symbols overlap the changed lines.")
        if report.get("unmapped_files"):
            click.echo("  unmapped files: " + ", ".join(report["unmapped_files"]))
        return
    imp = report["impact"]
    click.echo(f"Changed symbols: {', '.join(s['name'] for s in syms)}")
    click.echo(f"Affected (blast radius): {imp['total_affected']}")
    for label, key in (("direct", "direct_callers"), ("transitive", "transitive"),
                       ("endpoints", "api_endpoints_reached"), ("tests", "tests_covering"),
                       ("cross-service", "cross_service_consumers")):
        if imp.get(key):
            click.echo(f"  {label + ':':14}" + ", ".join(imp[key]))
    for flag in report.get("flags", []):
        click.echo(click.style(f"  ⚠ {flag}", fg="yellow"))
    if report.get("unmapped_files"):
        click.echo("  unmapped files: " + ", ".join(report["unmapped_files"]))


def _graph_of(folder) -> str:
    """The graph `sync` writes for `folder`: its registered name, else the default for it."""
    from auryn.core.app_config import get_repo
    from auryn.core.project import default_graph_name

    reg = get_repo(str(folder))
    return reg.graph_name if reg and reg.graph_name else default_graph_name(folder)


@cli.command("impact")
@click.option("--base", default=None, help="Base git ref (e.g. origin/main). With --head, diffs base..head; alone, base vs the working tree.")
@click.option("--head", default=None, help="Head git ref (use with --base).")
@click.option("--staged", is_flag=True, default=False, help="Use staged changes instead of all uncommitted changes.")
@click.option("--path", default=None, help="Repository directory to diff. Default: CLAUDE_PROJECT_DIR, else the current directory.")
@click.option("--graph-name", default=None, help="Graph to resolve against. Default: the graph of that repository.")
@click.option("--max-depth", default=4, type=int, show_default=True, help="Max reverse-dependency hops.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw JSON report (for a PR bot).")
@click.option("--fail-on", type=click.Choice(["none", "any", "cross-service", "endpoint"]), default="none",
              show_default=True, help="Exit non-zero when the blast radius trips this (CI merge gate).")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend (local starts on demand).")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
def impact(base, head, staged, path, graph_name, max_depth, as_json, fail_on,
           host, port, username, password, ssl) -> None:
    """"What will this change break?" -- the blast radius of a diff. Diffs the working tree (or a
    git range via --base/--head, or --staged), maps changed lines to graph symbols, and reports
    the reverse-dependency closure: callers, API endpoints, tests, and cross-service consumers.
    Use --fail-on to gate a PR in CI (exits non-zero). See auryn/core/impact.py."""
    import json as _json
    import subprocess
    from pathlib import Path

    from auryn.core import impact as impact_mod
    from auryn.core.app_config import load_config
    from auryn.core.project import resolve_project_root

    target = resolve_project_root(path)
    try:
        top = Path(impact_mod.git_toplevel(str(target))).resolve()
        if not impact_mod.parse_unified_diff(impact_mod.git_diff(str(top), base, head, staged=staged))[0]:
            click.echo("No changes in the given range.")  # nothing to check: no graph needed
            return
        client = connect_client(_resolve_connection(host, port, username, password, ssl))
        indexed = client.list_graphs()
        # The indexed folder the target belongs to: the nearest registered directory with a graph
        # from the target up to the top of ITS git repository, else that top -- so a run from a
        # subfolder still means the repo. Never above it: a plain folder registered higher up, or
        # an outer repository around a nested one, is a different source with a different diff.
        # A registration alone is no index: `auryn hooks ... --repo` registers a folder it never indexed.
        chain = [target, *target.parents]
        registered = {r.local_dir for r in load_config().repos}
        root = next((d for d in chain[:chain.index(top) + 1] if str(d) in registered and _graph_of(d) in indexed), top)
        diff = impact_mod.git_diff(str(root), base, head, staged=staged, relative=True)
    except subprocess.CalledProcessError as e:
        reason = ((e.stderr or "").strip().splitlines() or [str(e)])[0]  # git's own first line
        raise click.ClickException(f"couldn't compute git diff in {target}: {reason}") from e
    changed, _deleted = impact_mod.parse_unified_diff(diff)
    if not changed:
        click.echo("No changes in the given range.")
        return

    # The graph of the repo being diffed, resolved the way `sync` does -- not the CURRENT
    # folder's, which is a different graph (or none) whenever `--path` points elsewhere.
    graph_name = graph_name or _graph_of(root)
    if graph_name not in indexed:  # querying a graph that isn't there is a server error
        raise click.ClickException(
            f"{root} isn't indexed (no graph {graph_name!r}). Index it first: auryn ingest dir {root}")
    graph = client.select_graph(graph_name)
    report = impact_mod.impact_of_diff(graph, changed, max_depth=max_depth)

    if as_json:
        click.echo(_json.dumps(report, indent=2))
    else:
        _render_impact(report)

    imp = report.get("impact", {})
    trip = (
        (fail_on == "any" and imp.get("total_affected"))
        or (fail_on == "cross-service" and imp.get("cross_service_consumers"))
        or (fail_on == "endpoint" and imp.get("api_endpoints_reached"))
    )
    if trip:
        raise SystemExit(2)


@cli.command("serve")
@click.option("--port", type=int, default=None, help="Port to bind. Default: a random free loopback port.")
@click.option("--db-path", default=None, help="RDB store path. Default: ~/.auryn/store.db.")
def serve(port: int | None, db_path: str | None) -> None:
    """Run the local bundled FalkorDB (falkordb-lite) in the foreground.

    Normally started automatically on demand (the other commands call ensure_server() for the
    local backend); run it directly to inspect or debug the local server. Binds loopback only
    and blocks until terminated (Ctrl-C / SIGTERM)."""
    from auryn.core.server_manager import run_server

    run_server(port, db_path)


@cli.command("stop")
def stop() -> None:
    """Stop the local bundled FalkorDB daemon if it's running (no-op for a remote backend).

    The background deepen and embed daemons stop first: they reconnect through ensure_server(),
    so one left running would start the local FalkorDB again for its next queued request."""
    from auryn.core.deepen.daemon import stop_deepen_daemon
    from auryn.core.embeddings.daemon import stop_embed_daemon
    from auryn.core.server_manager import stop_server

    # Deepen before embed: a deep pass re-runs the ingest, which queues an embed request.
    if stop_deepen_daemon():
        click.echo("Stopped the background deepen daemon.")
    if stop_embed_daemon():
        click.echo("Stopped the background embed daemon.")
    click.echo("Stopped the local FalkorDB." if stop_server() else "No local FalkorDB running.")


def _prompt_backend(backend, host, port, username, password, ssl, assume_yes):
    """Resolve the backend for `install`. With ``assume_yes`` (``-y``) nothing is prompted:
    the backend auto-selects to ``--backend`` if given else ``local``, and any unspecified
    remote connection detail falls back to a default (localhost:6379, no creds). Without it,
    the choice and any unspecified detail are asked interactively."""
    from auryn.core.app_config import Backend
    from auryn.core.platform_support import LOCAL_UNSUPPORTED_MESSAGE, local_backend_supported

    if not local_backend_supported():
        # Native Windows can't run the local backend -- remote is the only option here. An explicit
        # --backend local is an error; otherwise force remote (and, non-interactively with no
        # connection given, tell them what to pass since we can't guess a server).
        if backend == "local":
            raise click.ClickException(LOCAL_UNSUPPORTED_MESSAGE)
        if backend is None:
            if assume_yes:
                raise click.ClickException(
                    "Native Windows can't run the local backend, so -y has nothing to auto-select. "
                    "Re-run with an explicit remote:\n"
                    "  auryn install -y --backend remote --host HOST --port PORT\n"
                    "or run auryn inside WSL2 (where the local backend works)."
                )
            click.echo("Native Windows detected — the local (falkordb-lite) backend isn't available here.")
            click.echo("Using a remote FalkorDB instead (start one in Docker/WSL2, or point at an existing server).")
            backend = "remote"

    if backend is None:
        if assume_yes:
            backend = "local"
        else:
            click.echo("How should auryn run FalkorDB?")
            click.echo("  1) Local  — bundled falkordb-lite, no server or Docker (recommended)")
            click.echo("  2) Remote — an existing FalkorDB server")
            backend = "remote" if click.prompt("Choose", type=click.Choice(["1", "2"]), default="1") == "2" else "local"
    if backend == "remote":
        if host is None:
            host = "localhost" if assume_yes else click.prompt("FalkorDB host", default="localhost")
        if port is None:
            port = 6379 if assume_yes else click.prompt("FalkorDB port", default=6379, type=int)
        if username is None and not assume_yes:
            username = click.prompt("Username (blank if none)", default="", show_default=False) or None
        if password is None and not assume_yes:
            password = click.prompt("Password (blank if none)", default="", hide_input=True, show_default=False) or None
        return Backend(mode="remote", host=host, port=port, username=username, password=password, ssl=ssl)
    return Backend(mode="local")


@cli.command("install")
@click.option("--backend", type=click.Choice(["local", "remote"]), default=None,
              help="FalkorDB backend. Prompted for if omitted.")
@click.option("--host", default=None, help="Remote FalkorDB host (with --backend remote).")
@click.option("--port", default=None, type=int, help="Remote FalkorDB port.")
@click.option("--username", default=None, help="Remote FalkorDB username.")
@click.option("--password", default=None, help="Remote FalkorDB password.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to the remote FalkorDB over TLS.")
@click.option("--trigger", type=click.Choice(["manual", "commit", "save"]), default=None,
              help="How the graph is kept fresh after an edit. Prompted for if omitted (default: the "
                   "saved setting, else commit).")
@click.option("--git-info", "git_info", type=click.Choice(["on", "off"]), default=None,
              help="Collect full local Git history and public GitHub metadata whenever a repo is ingested.")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Non-interactive: auto-select the backend (local unless --backend is given) and skip all prompts.")
def install(backend, host, port, username, password, ssl, trigger, git_info, yes) -> None:
    """Set up Auryn on this machine: choose a FalkorDB backend and register the MCP server
    with every detected agent (Claude Code, OpenAI Codex, Antigravity, GitHub Copilot, Cursor,
    Windsurf, Gemini CLI), user-scoped. Writes only to ~/.auryn and the agent's own config —
    never your repo. Re-runnable."""
    import tomllib

    from auryn.core import watcher
    from auryn.core.app_config import load_config, set_git_info_enabled, set_resync_trigger
    from auryn.core.home import config_path
    from auryn.core.install import default_server_command, do_install

    try:
        # Read first: a trigger that isn't passed keeps the saved one (a re-run must not turn a
        # `save` trigger back into `commit`), and a config.toml we can't read has to stop the
        # install before any agent is registered against it.
        saved = load_config()
    except Exception as e:  # noqa: BLE001 -- invalid TOML, or a section / field of the wrong shape
        # The type name, and of a TOML error only where it is. Their text can quote the file -- a
        # URL typed as the port, password and all, or as a table name -- and the repr holds more.
        reason = type(e).__name__
        if isinstance(e, tomllib.TOMLDecodeError):  # "... (at line 2, column 23)"
            reason += f" at {str(e).rpartition(' (at ')[2].removesuffix(')')}"
        raise click.ClickException(
            f"{config_path()} can't be read ({reason}). Fix or remove it, then re-run `auryn install`.") from e
    chosen = _prompt_backend(backend, host, port, username, password, ssl, yes)
    if trigger is None:
        trigger = saved.resync_trigger if yes else click.prompt(
            "Keep the graph fresh on", type=click.Choice(["commit", "save", "manual"]),
            default=saved.resync_trigger)
    if git_info is None:  # kept as saved too: turning it off purges the history already collected
        git_info = ("on" if saved.git_info_enabled else "off") if yes else ("on" if click.confirm(
            "Collect full Git history and public GitHub issues, pull requests, releases, and tags?",
            default=saved.git_info_enabled,
        ) else "off")
    was_git_info_enabled = load_config().git_info_enabled
    config = do_install(chosen)
    set_resync_trigger(trigger)
    watcher.ensure_watcher()  # `uninstall` stopped it, and kept the save-triggered repos on record
    set_git_info_enabled(git_info == "on")
    if was_git_info_enabled and git_info == "off":
        _purge_git_info_backend()
    # Semantic embeddings are always on (built in the background, off the ingest critical path) --
    # no install choice for it.
    click.echo("Semantic embeddings: on (built in the background on every ingest)")
    click.echo(f"Resync trigger: {trigger}"
               + {"commit": " (a git hook resyncs on each commit)",
                  "save": " (a watcher resyncs on save)",
                  "manual": " (resync on demand: `auryn sync`)"}[trigger])
    click.echo(f"Git information: {'on' if git_info == 'on' else 'off'}"
               + (" (local history plus public GitHub metadata on ingest)" if git_info == "on" else ""))

    if chosen.mode == "remote":
        click.echo(f"Backend: remote FalkorDB at {chosen.host}:{chosen.port}")
    else:
        click.echo("Backend: local bundled falkordb-lite (starts on demand; no server/Docker)")
    if config.agents:
        for a in config.agents:
            where = f"; skill → {a.skill_path}" if a.skill_path else (f"; config → {a.config_path}" if a.config_path else "")
            click.echo(f"Registered Auryn with {a.name} ({a.scope} scope){where}")
        click.echo("Restart your agent, then ask a structural question — it indexes the repo on first use.")
    else:
        click.echo("No supported agents detected. Register manually with Claude Code:")
        click.echo(f"  claude mcp add -s user auryn -- {' '.join(default_server_command())}")


@cli.command("uninstall")
@click.option("--purge", is_flag=True, default=False, help="Also delete ~/.auryn (config + ALL graphs).")
@click.option("--force", is_flag=True, default=False,
              help="Finish even when an agent can't be deregistered (its CLI is gone for good): forget it anyway.")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def uninstall(purge, force, yes) -> None:
    """Reverse `install`: stop the local server and deregister Auryn from every agent it was
    registered with. Keeps ~/.auryn (your graphs) unless --purge. Your repos are untouched."""
    from auryn.core import watcher
    from auryn.core.app_config import load_config
    from auryn.core.install import do_uninstall

    if not yes:
        msg = "Deregister Auryn from all agents and stop the local server?"
        if purge:
            msg += " This also DELETES ~/.auryn (all graphs)."
        if not click.confirm(msg, default=True):
            return
    watcher.stop_watcher()
    recorded = load_config().agents
    removed = do_uninstall(purge=purge, force=force).agents
    failed = ", ".join(a.name for a in recorded if a not in removed)
    if removed or not failed:
        click.echo(f"Deregistered from: {', '.join(a.name for a in removed) or '(no agents were registered)'}")
    if failed and not force:  # they stay on record, so the next run can retry
        raise click.ClickException(
            f"Could not deregister from: {failed} (see the warning above). Kept on record"
            + (", and ~/.auryn with it" if purge else "")
            + f" -- run `auryn uninstall{' --purge' if purge else ''}` again once that is fixed, or with --force"
            " to forget it anyway.")
    if failed:
        click.echo(f"Forgot without deregistering: {failed} -- remove Auryn from it by hand.", err=True)
    if purge:
        click.echo("Removed ~/.auryn.")
    click.echo("Remove the CLI itself with:  uv tool uninstall auryn")


@cli.group("git-info")
def git_info_group() -> None:
    """Manage collection of local Git history and public GitHub metadata."""


@git_info_group.command("sync")
@click.argument("path", required=False, default=".")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend.")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.option("--graph", "graph_name", default=None, help="Target graph. Default: resolved from PATH.")
@click.option("--force-remote/--no-force-remote", default=True,
              help="Refresh GitHub metadata now (default), or respect the once-a-day throttle.")
@click.option("--source-uri", "source_uri", default=None,
              help="Canonical source URI (e.g. the clone URL) when it differs from PATH; links "
                   "provenance to the code graph's Project.repo_uri. Default: PATH.")
def git_info_sync(path, host, port, username, password, ssl, graph_name, force_remote, source_uri) -> None:
    """Collect all locally reachable Git history and public GitHub metadata for PATH.

    Runs synchronously (this process does the work) and is single-flight per graph: if a sync is
    already running for the same graph it exits, so overlapping triggers never collect twice. Code
    ingestion spawns this DETACHED, so a one-shot `auryn ingest` doesn't cut the collection short."""
    import dataclasses
    import hashlib
    from pathlib import Path
    from auryn.core.app_config import get_repo, load_config
    from auryn.core.git_info import sync_git_info
    from auryn.core.home import run_dir
    from auryn.core.project import default_graph_name, resolve_project_root
    try:
        import fcntl
    except ImportError:  # pragma: no cover -- native Windows: no single-flight lock, just run
        fcntl = None

    if not load_config().git_info_enabled:
        # Honor the opt-out: a trigger (the watcher, a commit/save hook) must not recreate the subgraph
        # after `git-info disable`. `git-info enable` flips the setting before it invokes this.
        click.echo("Git information is disabled; skipping. Run `auryn git-info enable` to turn it on.")
        return

    root = Path(resolve_project_root(path))
    if not graph_name:
        reg = get_repo(str(root))
        graph_name = reg.graph_name if reg and reg.graph_name else default_graph_name(root)
    config = dataclasses.replace(_resolve_connection(host, port, username, password, ssl), graph_name=graph_name)

    run_dir().mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(graph_name.encode()).hexdigest()[:16]
    lock = open(run_dir() / f"git-info-{key}.lock", "w")  # noqa: SIM115 -- held for the sync's duration
    if fcntl is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            click.echo("Git information sync already running for this graph; skipping.")
            lock.close()
            return
    try:
        summary = sync_git_info(GraphWriter(connect(config)), str(root),
                                source_uri=source_uri or str(root), force_remote=force_remote)
        click.echo(f"Git information synced: {summary}")
    finally:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


@git_info_group.command("enable")
@click.argument("path", required=False, default=".")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend.")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.pass_context
def git_info_enable(ctx, path, host, port, username, password, ssl) -> None:
    """Enable Git information collection and sync PATH immediately."""
    from auryn.core.app_config import set_git_info_enabled
    set_git_info_enabled(True)
    ctx.invoke(git_info_sync, path=path, host=host, port=port, username=username,
               password=password, ssl=ssl)


@git_info_group.command("disable")
def git_info_disable() -> None:
    """Disable Git information collection on future ingests."""
    from auryn.core.app_config import set_git_info_enabled
    removed = _purge_git_info_backend()
    set_git_info_enabled(False)
    click.echo(f"Git information disabled; removed {removed} provenance nodes from the configured backend.")


def _git_info_watch_targets() -> list[str]:
    """Every registered repo's local checkout that still exists on disk, de-duplicated by resolved
    path. This is the enumeration the multi-project watcher refreshes. The registry (keyed by local
    checkout, ADR 0003) is the right source: git-info needs a local path to read `git log`, so a
    *remote-only* ingest -- which has a graph `Project` node but no checkout -- isn't a target anyway.
    Co-located repos in one graph each have their own registry entry, so they are all enumerated."""
    from pathlib import Path
    from auryn.core.app_config import load_config
    seen: set[str] = set()
    targets: list[str] = []
    for reg in load_config().repos:
        p = Path(reg.local_dir)
        if p.is_dir():
            resolved = str(p.resolve())
            if resolved not in seen:
                seen.add(resolved)
                targets.append(resolved)
    return targets


@git_info_group.command("watch")
@click.argument("path", required=False, default=None)
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend.")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.option("--graph", "graph_name", default=None,
              help="Target graph (single-repo mode). Default: resolved from PATH.")
@click.option("--interval", default=3600, type=int, show_default=True,
              help="Seconds between GitHub refreshes.")
@click.pass_context
def git_info_watch(ctx, path, host, port, username, password, ssl, graph_name, interval) -> None:
    """Periodically refresh GitHub metadata so new issues/PRs/releases/CI are picked up even when the
    code isn't changing (code-triggered syncs otherwise refresh GitHub at most once a day).

    With no PATH (and no --graph) this is a MULTI-PROJECT daemon: each interval it refreshes EVERY
    registered repository across EVERY graph, re-reading the registry each tick so newly ingested repos
    are picked up automatically. Pass a PATH to watch a single checkout instead.

    One watcher runs per machine -- a second invocation exits immediately. Within a graph, co-located
    repos are refreshed sequentially so they never trip the per-graph single-flight lock. Runs in the
    foreground -- run it under your process manager (systemd, nohup, a container) alongside the ingest
    client."""
    import signal
    import time
    from auryn.core.home import run_dir
    try:
        import fcntl
    except ImportError:  # pragma: no cover -- native Windows: no single-instance guard, just run
        fcntl = None

    multi = path is None and not graph_name

    # Hard single-instance guard (one watcher per machine). A flock, not a bare pidfile, so a crashed
    # watcher's lock is released by the OS automatically -- no stale-pid races.
    run_dir().mkdir(parents=True, exist_ok=True)
    guard = open(run_dir() / "git-info-watch.lock", "w")  # noqa: SIM115 -- held for the daemon's life
    if fcntl is not None:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            click.echo("A git-info watcher is already running; exiting.")
            guard.close()
            return

    stop = {"flag": False}

    def _handle(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    scope = "all registered repositories" if multi else (path or ".")
    click.echo(f"Watching GitHub activity for {scope} every {interval}s (Ctrl-C to stop).")
    try:
        while not stop["flag"]:
            if multi:
                targets = [(t, None) for t in _git_info_watch_targets()]
                if not targets:
                    click.echo("No registered repositories yet; will re-check next interval.")
            else:
                targets = [(path or ".", graph_name)]
            for target_path, target_graph in targets:  # sequential: never collides with the per-graph lock
                if stop["flag"]:
                    break
                try:
                    ctx.invoke(git_info_sync, path=target_path, host=host, port=port,
                               username=username, password=password, ssl=ssl,
                               graph_name=target_graph, force_remote=True)
                except Exception as exc:  # noqa: BLE001 -- one repo's failure must not stop the rest
                    click.echo(f"Refresh failed for {target_path} ({exc}); continuing.", err=True)
            for _ in range(max(interval, 1)):  # sleep in 1s steps so Ctrl-C is responsive
                if stop["flag"]:
                    break
                time.sleep(1)
    finally:
        if fcntl is not None:
            fcntl.flock(guard, fcntl.LOCK_UN)
        guard.close()
    click.echo("Stopped watching GitHub activity.")


def _conn_options(fn):
    """Standard FalkorDB connection flags (shared by list/forget). With no --host, the
    configured backend is used (local starts on demand)."""
    fn = click.option("--host", default=None, help="FalkorDB host. Default: the configured backend.")(fn)
    fn = click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")(fn)
    fn = click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")(fn)
    fn = click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")(fn)
    fn = click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")(fn)
    return fn


@cli.command("list")
@_conn_options
def list_repos(host, port, username, password, ssl) -> None:
    """List the repos ingested into the graph backend, with their languages, commit, and size."""
    from auryn.core import lifecycle

    client = connect_client(_resolve_connection(host, port, username, password, ssl))
    graphs = lifecycle.list_ingested(client)
    if not graphs:
        click.echo("No repos ingested yet.")
        return
    for gi in graphs:
        click.echo(f"graph {gi.graph_name}  ({gi.nodes} nodes, {gi.edges} edges)")
        for p in gi.projects:
            shas = ", ".join(f"{lang} {sha}" for lang, sha in p.commit_shas.items())
            click.echo(f"  - {p.name}  [{', '.join(p.languages)}]  {shas}")


@cli.command("stats")
@click.option("--since", default=None, help="Only events within a window (e.g. 30m, 24h, 7d) or since an ISO date.")
@click.option("--graph", default=None, help="Only events for this graph name.")
@click.option("--by", "group_by", type=click.Choice(["tool", "op", "session", "graph"]),
              default="tool", show_default=True, help="How to group the breakdown.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw summary as JSON.")
def stats(since, graph, group_by, as_json) -> None:
    """How an agent is using the code graph: how many times it called the MCP tools and which
    operations -- visibility into how well the graph is being used. Reads the local usage log
    (~/.auryn/usage.jsonl); nothing is sent anywhere."""
    import json

    from auryn.core import usage

    events = usage.read_events(since=since, graph=graph)
    if not events:
        extra = f" (since={since}, graph={graph})" if (since or graph) else ""
        click.echo("No graph usage recorded yet." + extra)
        return

    summary = usage.summarize(events)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
        return

    reads, writes = summary["by_op"].get("read", 0), summary["by_op"].get("write", 0)
    total, errs = summary["total"], summary["errors"]
    rate = f"{100 * errs / total:.0f}%" if total else "0%"
    click.echo(f"Code graph accesses: {total}   ({reads} read, {writes} write)")
    click.echo(f"Sessions: {summary['sessions']}   Errors: {errs} ({rate})   Graphs: {len(summary['graphs'])}")
    if summary["first"]:
        click.echo(f"Window: {summary['first']} → {summary['last']}")
    click.echo("")

    if group_by == "tool":
        click.echo(f"  {'operation':<22}{'op':<7}{'calls':>6}{'err':>5}{'avg ms':>8}{'avg result':>12}")
        for name, d in sorted(summary["by_tool"].items(), key=lambda kv: -kv[1]["count"]):
            avg_ms = d["ms"] / d["count"] if d["count"] else 0
            res = f"{d['result'] / d['result_n']:.0f}" if d["result_n"] else "-"
            click.echo(f"  {name:<22}{d['op']:<7}{d['count']:>6}{d['errors']:>5}{avg_ms:>8.0f}{res:>12}")
    else:
        click.echo(f"  {group_by:<40}{'calls':>6}")
        for key, n in usage.group_counts(events, group_by).items():
            click.echo(f"  {key:<40}{n:>6}")


@cli.command("forget")
@click.argument("path", required=False)
@click.option("--graph", "graph_opt", default=None, help="Forget by graph name instead of a path.")
@click.option("--project", default=None, help="In a shared multi-repo graph, the project to forget.")
@_conn_options
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be deleted, then stop.")
def forget(path, graph_opt, project, host, port, username, password, ssl, yes, dry_run) -> None:
    """Delete an ingested repo from the graph -- the counter to `ingest`.

    Targets the repo at PATH (default: the current directory), or a graph by --graph. A
    dedicated graph (the default one-graph-per-repo) is dropped wholesale; in a shared graph,
    pass --project to remove just that repo's subgraph (inbound cross-repo edges are cleaned up
    too). Your files are never touched."""
    from auryn.core import lifecycle
    from auryn.core.app_config import get_repo
    from auryn.core.project import default_graph_name, resolve_project_root

    client = connect_client(_resolve_connection(host, port, username, password, ssl))
    # Resolve the target graph the same way `sync` does: the registry is authoritative (a repo may
    # have been ingested into a shared/renamed graph via --graph-name), falling back to the derived
    # per-repo graph. For a registered repo in a shared graph, default --project to its name so
    # `forget <path>` just works instead of demanding --project.
    if graph_opt:
        graph_name = graph_opt
    else:
        root = resolve_project_root(path)
        reg = get_repo(str(root))
        if reg and reg.graph_name:
            graph_name = reg.graph_name
            if project is None:
                project = reg.project_name or root.name
        else:
            graph_name = default_graph_name(root)
    try:
        plan = lifecycle.plan_forget(client, graph_name, project)
    except lifecycle.ForgetError as e:
        raise click.ClickException(str(e)) from e

    target = (f"graph {plan.graph_name!r}" if plan.mode == "drop_graph"
              else f"project {plan.project_name!r} in graph {plan.graph_name!r}")
    click.echo(f"Will delete {target}: {plan.nodes} nodes, languages [{', '.join(plan.languages)}].")
    if dry_run:
        return
    if not yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted.")
        return
    lifecycle.execute_forget(client, plan)
    _deregister_repo(path, graph_opt, project, plan.graph_name)
    click.echo(f"Forgot {target}.")


def _deregister_repo(path, graph_opt, project, graph_name) -> None:
    """Remove a forgotten repo's git hooks + registry entry."""
    from auryn.core import app_config, hooks
    from auryn.core.project import resolve_project_root

    if not graph_opt:
        local_dir = str(resolve_project_root(path))
    else:  # forgot by --graph: find the registry entry that maps there
        match = next((r for r in app_config.load_config().repos
                      if r.graph_name == graph_name and (project is None or r.project_name == project)), None)
        local_dir = match.local_dir if match else None
    if local_dir:
        hooks.uninstall(local_dir)
        app_config.unregister_repo(local_dir)


@cli.command("sync")
@click.argument("path", required=False)
@_conn_options
@click.option("--deep", type=click.Choice(["off", "inline", "background"]), default="off",
              help="Deep pass (default: off = fast incremental, the freshness path).")
def sync(path, host, port, username, password, ssl, deep) -> None:
    """Resync a repo's graph incrementally — the target of the commit hook / save watcher, and
    runnable by hand. Re-ingests the working directory; only the change's blast radius is touched."""
    import dataclasses

    from auryn.core.app_config import get_repo
    from auryn.core.ingest import ingest_directory
    from auryn.core.project import default_graph_name, resolve_project_root
    from auryn.core.sync_lock import sync_lock

    root = resolve_project_root(path)
    # One sync per repo at a time: the git hooks fire a burst of these (a rebase starts one per
    # commit) and overlapping syncs of one project corrupt its subgraph. See core/sync_lock.py.
    # Only the plain hook-shaped sync may step aside for a queued one -- a deep pass or another
    # server is work the queued sync won't do, so that request waits for its own turn.
    plain = deep == "off" and not any((host, port, username, password, ssl))
    with sync_lock(root, queue_one=plain) as my_turn:
        if not my_turn:
            click.echo(f"A sync of {root} is already queued; it will pick up this change.")
            return
        reg = get_repo(str(root))
        graph = reg.graph_name if reg and reg.graph_name else default_graph_name(root)
        project = reg.project_name if reg else None
        config = dataclasses.replace(_resolve_connection(host, port, username, password, ssl), graph_name=graph)
        # An explicit --host is an ad-hoc connection the background daemon (which reconnects via
        # the configured backend) might not match, so embed inline there; otherwise let it
        # resolve to the default (background). The context manager holds the lock for any `--deep
        # background` writers (as a finally would) and collects which failed, so a failed background
        # pass is surfaced like an inline one (#857).
        with _surfaced_background_deep() as bg:
            report = ingest_directory(GraphWriter(connect(config)), str(root), project_name=project,
                                      deep=deep, auto_install=False, embed=("inline" if host else None))
    if _echo_ingest_report(report, background_failures=bg["failed"]):
        raise SystemExit(1)  # a deep pass failed -> the graph is shallow; signal it, don't exit 0 (#857)


@cli.command("watch")
@click.option("--foreground", is_flag=True, default=False,
              help="Run the watcher in this process (blocks); default: manage the background daemon.")
@click.option("--stop", "stop_", is_flag=True, default=False, help="Stop the background watcher.")
@click.option("--status", is_flag=True, default=False, help="Report whether the watcher is running.")
def watch(foreground, stop_, status) -> None:
    """The save-trigger watcher: resyncs a repo's graph when its files change. Started
    automatically for `save`-triggered repos; this command manages it explicitly."""
    from auryn.core import watcher

    if status:
        click.echo("Watcher is running." if watcher.is_watcher_running() else "Watcher is not running.")
        return
    if stop_:
        click.echo("Watcher stopped." if watcher.stop_watcher() else "Watcher was not running.")
        return
    if foreground:
        watcher.run_watcher()  # blocks until SIGTERM/SIGINT
        return
    if watcher.ensure_watcher():
        click.echo("Watcher is running.")
    else:
        click.echo("No save-triggered repos to watch.")


def _repo_basename(uri: str) -> str:
    """The directory `git clone <uri>` would create: the last path segment, minus a `.git`."""
    base = uri.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".git") else (base or "repo")


def _wire_trigger(local_dir, graph_name: str, project_name: str | None, trigger: str) -> None:
    """Register the repo and install its resync trigger (ADR 0003), echoing what happened. Thin
    wrapper over `lifecycle.wire_trigger` so the CLI and the MCP index tools share ONE
    implementation -- the MCP can't drift from the CLI (e.g. silently skip hook installation)."""
    from auryn.core import lifecycle

    status = lifecycle.wire_trigger(local_dir, graph_name, project_name, trigger)
    if not status["registered"]:
        click.echo(f"({status['note']})")
        return
    if status["trigger"] == "commit":
        if status.get("hooks_installed"):
            click.echo("Resync on commit: git hooks installed.")
        else:
            click.echo(f"({status.get('note', 'commit hook skipped')})")
    elif status["trigger"] == "save":
        if status.get("watcher_running"):
            click.echo("Resync on save: watcher daemon running.")
        else:
            click.echo("(save trigger recorded; watcher will start when watchable)")


@click.group("git-sync")
@click.option("--host", default="localhost", show_default=True, help="FalkorDB host.")
@click.option("--port", default=6379, show_default=True, type=int, help="FalkorDB port.")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.option("--graph-name", default="auryn", show_default=True, help="Target graph name.")
@click.pass_context
def git_sync(
    ctx: click.Context,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    ssl: bool,
    graph_name: str,
) -> None:
    """Ingest, or move to a different commit/branch/tag, a git-tracked project's graph."""
    ctx.obj = {
        "falkordb_config": FalkorDBConfig(
            host=host, port=port, username=username, password=password, ssl=ssl, graph_name=graph_name
        )
    }


@git_sync.command("csharp")
@click.option("--repo-uri", required=True, help="Git repository URI to clone/fetch (anything `git` itself understands).")
@click.option(
    "--project-name", required=True, help="Logical name for this project, used to build the Project node's id."
)
@click.option("--solution-name", default=None, help="Optional: wraps the Project in a Solution node.")
@click.option("--target-framework", default=None, help="Free-text, e.g. net8.0 -- informational only.")
@click.option(
    "--ref", default=None, help="Branch, tag, or commit sha to sync to. Default: the repo's current default branch."
)
@click.option(
    "--dto-suffix",
    "dto_suffixes",
    multiple=True,
    help="DTO name-suffix heuristic (repeatable). Default: Dto, Request, Response, ViewModel.",
)
@click.option(
    "--workers", default=None, type=int, help="Process pool size for parallel per-file parsing. Default: os.cpu_count()."
)
@click.option(
    "--engine",
    type=click.Choice(["roslyn", "treesitter"]),
    default="roslyn",
    show_default=True,
    help="C# extractor: 'roslyn' (semantic + DI resolution; needs the .NET SDK + extractor tool, "
    "falls back to tree-sitter if unavailable) or 'treesitter' (syntax-only).",
)
@click.pass_context
def git_sync_csharp(
    ctx: click.Context,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    target_framework: str | None,
    ref: str | None,
    dto_suffixes: tuple[str, ...],
    workers: int | None,
    engine: str,
) -> None:
    """Ingest (first run) or move (every run after) a git-tracked C# project's graph to --ref."""
    from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync

    writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
    config = GitCSharpProjectConfig(
        repo_uri=repo_uri,
        project_name=project_name,
        solution_name=solution_name,
        target_framework=target_framework,
        dto_name_suffixes=tuple(dto_suffixes) or GitCSharpProjectConfig.dto_name_suffixes,
        workers=workers,
    )
    stats = sync(writer, config, ref, engine=engine)
    click.echo(
        f"Synced to commit {stats.commit_sha} in {stats.elapsed_seconds:.2f}s: "
        f"nodes +{stats.nodes_added}/-{stats.nodes_removed}/~{stats.nodes_updated}, "
        f"edges +{stats.edges_added}/-{stats.edges_removed}/~{stats.edges_updated}"
    )


@git_sync.command("plsql")
@click.option("--repo-uri", required=True, help="Git repository URI to clone/fetch (anything `git` itself understands).")
@click.option("--project-name", required=True, help="Logical name for this project, used to build node ids.")
@click.option("--solution-name", default=None, help="Optional: wraps the Project in a Solution node.")
@click.option(
    "--ref", default=None, help="Branch, tag, or commit sha to sync to. Default: the repo's current default branch."
)
@click.option(
    "--workers", default=None, type=int, help="Process pool size for parallel per-file parsing. Default: os.cpu_count()."
)
@click.pass_context
def git_sync_plsql(
    ctx: click.Context,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    workers: int | None,
) -> None:
    """Ingest (first run) or move (every run after) a git-tracked PL/SQL project's graph to --ref."""
    from auryn.ingestors.plsql.config import GitPLSQLProjectConfig
    from auryn.ingestors.plsql.git_sync import sync as plsql_sync

    writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
    config = GitPLSQLProjectConfig(
        repo_uri=repo_uri, project_name=project_name, solution_name=solution_name, workers=workers
    )
    stats = plsql_sync(writer, config, ref)
    click.echo(
        f"Synced to commit {stats.commit_sha} in {stats.elapsed_seconds:.2f}s: "
        f"nodes +{stats.nodes_added}/-{stats.nodes_removed}/~{stats.nodes_updated}, "
        f"edges +{stats.edges_added}/-{stats.edges_removed}/~{stats.edges_updated}"
    )


@click.group("ingest")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend (local starts on demand).")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.option("--graph-name", default=None,
              help="Target graph. Default: one per repo/dir. Set it to ingest several codebases into one graph.")
@click.pass_context
def ingest_group(
    ctx: click.Context,
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
    ssl: bool,
    graph_name: str | None,
) -> None:
    """Ingest every supported language from one source into a graph, in a single pass.

    The CLI twin of the MCP ingest tools: auto-detects each installed file-based
    language plugin (currently C, C++, C#, Python, Java, Go, Rust, TypeScript/JavaScript,
    Ruby, PHP, Kotlin, Swift, Julia, Bash, PL/SQL) and ingests them all, then runs the
    enrichment passes. Progress is logged live to stderr as each language runs."""
    # Stash raw options only -- do NOT resolve the connection here: click runs this group
    # callback even for `ingest dir --help`, and resolving would start the local daemon. The
    # subcommand resolves it (and the per-repo graph name) once it's actually about to ingest.
    ctx.obj = {"host": host, "port": port, "username": username, "password": password,
               "ssl": ssl, "graph_name": graph_name}


def _ingest_writer(ctx: click.Context, graph_name: str) -> GraphWriter:
    """Resolve the connection (lazily starting the local daemon if needed) and bind a writer to
    `graph_name`. Called from the subcommands, so the daemon starts only for a real ingest."""
    import dataclasses

    o = ctx.obj
    config = dataclasses.replace(
        _resolve_connection(o["host"], o["port"], o["username"], o["password"], o["ssl"]),
        graph_name=graph_name,
    )
    return GraphWriter(connect(config))


def _surfaced_background_deep():
    """A `--deep background` failure must be surfaced + exit non-zero like an inline one (#857).
    Thin alias for ingest.deep_failure_scope() (which collects THIS ingest's deep threads race-free
    and exposes the failed ones on exit) -- kept so the call sites read locally. Use as
    `with _surfaced_background_deep() as bg: report = ...` then pass bg["failed"] to the report."""
    from auryn.core import ingest as _ingest
    return _ingest.deep_failure_scope()


def _echo_ingest_report(report, background_failures: "set[str] | None" = None) -> list[str]:
    """A compact end-of-run summary echoed to stdout. The blow-by-blow progress has
    already streamed to stderr via the `auryn.ingest` logger (setup_logging on the
    top-level group), so this is just the final tally.

    Returns the languages whose DEEP (semantic) pass FAILED. A deep failure leaves a SHALLOW
    graph (`depth=fast`) that otherwise looks fine in the summary, so it is surfaced loudly here
    (and the caller exits non-zero) -- otherwise a timed-out/erroring deep pass is invisible and
    an agent or user queries an incomplete graph without knowing (#857).

    `background_failures` are languages whose `--deep background` pass failed AFTER the report was
    built (collected by ingest.deep_failure_scope()); the report still records them as "background",
    so the caller passes them in here to be surfaced as failures too."""
    bg_failed = background_failures or set()
    d = report.to_dict()
    click.echo(f"Ingested {report.project_name!r} ({report.commit_sha[:12]}) in {d['elapsed_seconds']:.2f}s")
    deep_failed: list[str] = []
    for lang in d["languages"]:
        if "error" in lang:
            click.echo(f"  {lang['language']}: ERROR {lang['error']}")
            continue
        line = (
            f"  {lang['language']}: nodes +{lang['nodes_added']}/-{lang['nodes_removed']}/~{lang['nodes_updated']}, "
            f"edges +{lang['edges_added']}/-{lang['edges_removed']}/~{lang['edges_updated']} "
            f"({lang['files_parsed']} parsed, {lang['files_cached']} cached)"
        )
        ds = lang.get("deep_status")
        if ds == "error" or lang["language"] in bg_failed:
            deep_failed.append(lang["language"])
            line += "  [deep: FAILED -> shallow]"
        elif ds and ds != "inline":  # inline success is the norm; surface async/background/off/skipped
            line += f"  [deep: {ds}]"
        click.echo(line)
    if d["unsupported_detected"]:
        detail = ", ".join(f"{u['language']} ({u['files']})" for u in d["unsupported_detected"])
        click.echo(f"  detected-but-unsupported: {detail}")
    if d["enrichments"]:
        click.echo(f"  enrichments: {d['enrichments']}")
    t = d["totals"]
    click.echo(
        f"Totals: nodes +{t['nodes_added']}/-{t['nodes_removed']}/~{t['nodes_updated']}, "
        f"edges +{t['edges_added']}/-{t['edges_removed']}/~{t['edges_updated']}"
    )
    if deep_failed:
        click.secho(
            f"\nWARNING: the deep (semantic) pass FAILED for {', '.join(deep_failed)} -- the graph is "
            "SHALLOW (depth=fast). Call resolution is name-based, so who_calls / callees / impact_of are "
            "INCOMPLETE. See the error logged above.",
            fg="yellow", err=True,
        )
        click.secho(
            "  Auryn already gives writes a long per-query timeout (clamped to the server's "
            "TIMEOUT_MAX) and splits-and-retries a timed-out batch, so a persistent FalkorDB 'Query "
            "timed out' here means the server is overloaded or stuck -- reduce concurrent load, or if "
            "a low TIMEOUT_MAX is pinning the budget raise it (GRAPH.CONFIG SET TIMEOUT_MAX 0), then "
            "re-ingest.",
            fg="yellow", err=True,
        )
    return deep_failed


# Repeatable allow/deny filters (folders + file types), shared by `dir` and `repo`. Directory
# names match at any path level; extensions may be given with or without a leading dot.
def _filter_options(fn):
    fn = click.option("--include-dir", "include_dirs", multiple=True,
                      help="Only ingest files under a directory with this name (repeatable; whitelist).")(fn)
    fn = click.option("--exclude-dir", "exclude_dirs", multiple=True,
                      help="Skip files under a directory with this name, e.g. deps (repeatable; blacklist).")(fn)
    fn = click.option("--include-ext", "include_suffixes", multiple=True,
                      help="Only ingest files with this extension, e.g. .c or py (repeatable; whitelist).")(fn)
    fn = click.option("--exclude-ext", "exclude_suffixes", multiple=True,
                      help="Skip files with this extension (repeatable; blacklist).")(fn)
    return fn


# Per-run post-hook overrides, shared by `ingest dir`/`ingest repo`. These layer on top of the
# persisted install-wide/per-repo disables (see `auryn hooks`): --skip-hook adds to the skip
# set for this run; --only-hook runs exactly those, ignoring any disable.
def _hook_options(fn):
    fn = click.option("--only-hook", "only_hooks", multiple=True,
                      help="Run only these post hook(s) after ingest, repeatable (overrides disables).")(fn)
    fn = click.option("--skip-hook", "skip_hooks", multiple=True,
                      help="Skip these post hook(s) after ingest, repeatable.")(fn)
    return fn


def _validate_hook_names(names) -> None:
    """Clean CLI error for a hook name that isn't registered (typo-catch)."""
    if not names:
        return
    from auryn.core.registry import discover_graph_enrichments
    known = discover_graph_enrichments()
    unknown = [n for n in names if n not in known]
    if unknown:
        avail = ", ".join(sorted(known)) or "(none registered)"
        raise click.ClickException(f"unknown hook(s): {', '.join(unknown)}. Available: {avail}")


_DEEP_OPTION = click.option(
    "--deep", type=click.Choice(["inline", "background", "async", "off"]), default="async",
    help="Deep semantic pass for two-phase languages (Python via Jedi, C# via Roslyn, Rust via "
         "rust-analyzer): async=return on the fast graph, deepen in a detached daemon that survives "
         "this process exiting (default -- the graph is queryable immediately; an explicit --host "
         "connection the daemon can't reach falls back to inline); inline=fast then deep, synchronous; "
         "off=fast structural only (much quicker on large repos); background=deepen in an in-process "
         "thread (long-lived processes only -- a one-shot CLI would kill it; prefer async).",
)


def _effective_deep(deep: str, host: str | None) -> str:
    """`deep="async"` hands the pass to a detached daemon that reconnects to the CONFIGURED backend;
    an explicit `--host` is an ad-hoc connection the daemon may not match, so deepen inline instead
    (mirrors the embed-mode fallback). Any other mode passes through unchanged."""
    return "inline" if deep == "async" and host else deep


@ingest_group.command("dir")
@click.argument("path")
@click.option("--project-name", default=None, help="Logical name for this project. Default: the directory's name.")
@click.option("--solution-name", default=None, help="Optional: wraps the Project in a Solution node.")
@click.option("--trigger", type=click.Choice(["manual", "commit", "save"]), default=None,
              help="Resync trigger for this repo. Default: the install-wide setting.")
@_filter_options
@_hook_options
@_DEEP_OPTION
@click.pass_context
def ingest_dir(ctx: click.Context, path: str, project_name: str | None, solution_name: str | None,
               trigger: str | None, include_dirs, exclude_dirs, include_suffixes, exclude_suffixes,
               only_hooks, skip_hooks, deep: str) -> None:
    """Ingest every supported file under a local directory PATH, recursively (all
    nested subfolders) -- no git required."""
    from auryn.core import ingest
    from auryn.core.app_config import load_config
    from auryn.core.project import default_graph_name, resolve_project_root
    from auryn.core.sync_lock import sync_lock

    _validate_hook_names(only_hooks + skip_hooks)
    graph = ctx.obj["graph_name"] or default_graph_name(resolve_project_root(path))
    writer = _ingest_writer(ctx, graph)
    try:
        # Its turn among this repo's hook / watcher syncs, which write the same project subgraph
        # (core/sync_lock.py) -- e.g. a deep re-ingest while a commit hook fires.
        # The context manager holds the lock for `--deep background` writers, as `sync` does, and
        # collects any that failed so a failed background pass is surfaced like an inline one (#857).
        with sync_lock(resolve_project_root(path), queue_one=False), _surfaced_background_deep() as bg:
            report = ingest.ingest_directory(
                writer, path, project_name=project_name, solution_name=solution_name,
                deep=_effective_deep(deep, ctx.obj.get("host")),
                # Explicit --host is an ad-hoc connection the background daemon may not match -> inline.
                embed=("inline" if ctx.obj.get("host") else None),
                include_dirs=include_dirs, exclude_dirs=exclude_dirs,
                include_suffixes=include_suffixes, exclude_suffixes=exclude_suffixes,
                enrichment_only=only_hooks, enrichment_skip=skip_hooks,
            )
    except ValueError as e:  # missing path / not a directory -- a clean CLI error, not a traceback
        raise click.ClickException(str(e)) from e
    deep_failed = _echo_ingest_report(report, background_failures=bg["failed"])
    _wire_trigger(str(resolve_project_root(path)), graph, project_name, trigger or load_config().resync_trigger)
    if deep_failed:
        raise SystemExit(1)  # deep pass failed -> shallow graph; signal it AFTER wiring the trigger (#857)


@ingest_group.command("repo")
@click.argument("repo_uri")
@click.option("--ref", default=None, help="Branch, tag, or commit sha. Default: the repo's current default branch.")
@click.option("--project-name", default=None, help="Logical name for this project. Default: the repo's name.")
@click.option("--solution-name", default=None, help="Optional: wraps the Project in a Solution node.")
@click.option("--trigger", type=click.Choice(["manual", "commit", "save"]), default=None,
              help="Resync trigger for this repo. Default: the install-wide setting.")
@_filter_options
@_hook_options
@_DEEP_OPTION
@click.pass_context
def ingest_repo(
    ctx: click.Context, repo_uri: str, ref: str | None, project_name: str | None, solution_name: str | None,
    trigger: str | None, include_dirs, exclude_dirs, include_suffixes, exclude_suffixes,
    only_hooks, skip_hooks, deep: str
) -> None:
    """Ingest a git repository REPO_URI at --ref. A REMOTE URI is `git clone`d into the current
    directory (like `git clone`), then that working copy is ingested — so save/commit triggers
    attach to a real local checkout. A local path is ingested in place."""
    import os
    import subprocess
    from pathlib import Path

    import git

    from auryn.core import hooks, ingest
    from auryn.core.app_config import load_config
    from auryn.core.project import default_graph_name, graph_name_for_repo, resolve_project_root

    _validate_hook_names(only_hooks + skip_hooks)
    _filters = dict(include_dirs=include_dirs, exclude_dirs=exclude_dirs,
                    include_suffixes=include_suffixes, exclude_suffixes=exclude_suffixes,
                    enrichment_only=only_hooks, enrichment_skip=skip_hooks)

    if not os.path.isdir(repo_uri):
        # Remote URI: clone into CWD (like `git clone`), then ingest the working checkout (ADR 0003).
        dest = Path.cwd() / _repo_basename(repo_uri)
        if dest.exists():
            raise click.ClickException(f"destination path {dest.name!r} already exists")
        try:
            subprocess.run(["git", "clone", repo_uri, str(dest)], check=True)
            if ref:
                subprocess.run(["git", "-C", str(dest), "checkout", ref], check=True)
        except subprocess.CalledProcessError as e:
            raise click.ClickException(f"git clone failed ({e.returncode})") from e
        click.echo(f"Cloned {repo_uri} → {dest}")
        graph = ctx.obj["graph_name"] or default_graph_name(dest)
        writer = _ingest_writer(ctx, graph)
        with _surfaced_background_deep() as bg:
            report = ingest.ingest_directory(writer, str(dest), project_name=project_name,
                                             solution_name=solution_name,
                                             deep=_effective_deep(deep, ctx.obj.get("host")),
                                             embed=("inline" if ctx.obj.get("host") else None), **_filters)
        deep_failed = _echo_ingest_report(report, background_failures=bg["failed"])
        _wire_trigger(str(dest.resolve()), graph, project_name, trigger or load_config().resync_trigger)
        if deep_failed:
            raise SystemExit(1)  # deep pass failed -> shallow graph; signal it (#857)
        return

    # Local path: ingest at --ref (bare-clone cache), then wire the trigger onto the checkout.
    graph = ctx.obj["graph_name"] or graph_name_for_repo(repo_uri)
    writer = _ingest_writer(ctx, graph)
    try:
        with _surfaced_background_deep() as bg:
            report = ingest.ingest_repository(
                writer, repo_uri, ref=ref, project_name=project_name, solution_name=solution_name,
                deep=_effective_deep(deep, ctx.obj.get("host")),
                embed=("inline" if ctx.obj.get("host") else None), **_filters
            )
    except (ValueError, git.GitCommandError) as e:  # bad ref / unreachable repo -> clean CLI error
        raise click.ClickException(str(e)) from e
    deep_failed = _echo_ingest_report(report, background_failures=bg["failed"])
    local_dir = str(resolve_project_root(repo_uri)) if hooks.is_git_repo(repo_uri) else None
    _wire_trigger(local_dir, graph, project_name, trigger or load_config().resync_trigger)
    if deep_failed:
        raise SystemExit(1)  # deep pass failed -> shallow graph; signal it (#857)


@cli.command("quickstart")
@click.argument("path", required=False)
@click.option("--db-path", default=None,
              help="File the embedded graph persists to. Default: <path>/.auryn/graph.db "
                   "(or a temp dir for the bundled sample).")
@click.option("--graph-name", default="auryn", show_default=True, help="Graph to ingest into and open.")
@click.option("--project-name", default=None, help="Logical project name. Default: the directory's name.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host the viewer binds to.")
@click.option("--port", default=8080, show_default=True, type=int, help="Port the viewer binds to.")
@click.option("--no-open", is_flag=True, default=False, help="Don't open the viewer in a browser.")
@_filter_options
@_DEEP_OPTION
def quickstart(path: str | None, db_path: str | None, graph_name: str, project_name: str | None, host: str, port: int,
               no_open: bool, include_dirs, exclude_dirs, include_suffixes, exclude_suffixes, deep: str) -> None:
    """Zero-setup: ingest PATH into an embedded FalkorDB and open the viewer -- no server, no Docker.

    With no PATH, graphs a tiny bundled sample app (a payments API shipped inside the
    package) so the first run is always instant and self-contained -- pass a PATH to graph
    your own code instead.

    One self-contained command: boots an in-process FalkorDB (the optional `embedded`
    extra -- falkordblite, auto-installed on first use), ingests every supported language
    under PATH, then serves the viewer over that same in-process graph. Ingestion and the
    viewer share one process on purpose: an embedded FalkorDB is owned by a single process,
    so this can't be split into separate `ingest`+`view` runs against the one file. For a
    shared/long-lived setup, run a real FalkorDB and use `ingest` + `view` instead."""
    from pathlib import Path

    from auryn.core import ingest
    from auryn.core.platform_support import local_backend_supported
    from auryn.viewer.app import create_app

    if not local_backend_supported():  # embedded falkordb-lite is POSIX-only; no remote fallback here
        raise click.ClickException(
            "quickstart needs the embedded falkordb-lite, which isn't supported on native Windows. "
            "Run it inside WSL2, or use a remote FalkorDB with the split workflow:\n"
            "  auryn install --backend remote --host HOST --port PORT\n"
            "  auryn ingest PATH  &&  auryn view"
        )

    if path is None:  # no PATH -> graph the bundled sample so `quickstart` is always fast & self-contained
        root = Path(__file__).resolve().parent / "assets" / "quickstart_sample"
        project_name = project_name or "quickstart"
        click.echo("No PATH given -- graphing the bundled sample app (a tiny payments API).")
        click.echo("Point it at your own code with:  auryn quickstart /path/to/your/repo\n")
    else:
        root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise click.ClickException(f"Not a directory: {root}")

    if db_path:
        db_file = Path(db_path).expanduser().resolve()
    elif path is None:
        # The bundled sample lives in read-only site-packages; keep its graph file out of there.
        import tempfile
        db_file = Path(tempfile.gettempdir()) / "auryn-quickstart" / "graph.db"
    else:
        db_file = root / ".auryn" / "graph.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)

    config = FalkorDBConfig(embedded=True, db_path=str(db_file), graph_name=graph_name)
    try:
        client = connect_client(config)  # may auto-install falkordblite on first run
    except ImportError as e:  # auto-install disabled/failed -> clean CLI error, not a traceback
        raise click.ClickException(str(e)) from e
    click.echo(f"Embedded FalkorDB (serverless) at {db_file}")

    writer = GraphWriter(client.select_graph(graph_name))
    try:
        # Surface a failed `--deep background` pass too (warn only -- quickstart goes on to serve the
        # graph rather than exit non-zero like the ingest/sync commands do). See #857.
        with _surfaced_background_deep() as bg:
            report = ingest.ingest_directory(
                writer, str(root), project_name=project_name,
                # Serverless/embedded DB lives in THIS process -- a detached daemon (deep=async /
                # background embed) couldn't reach it, so deepen AND embed inline here rather than
                # enqueuing a job that would find nothing.
                deep=("inline" if deep == "async" else deep),
                embed="inline",
                include_dirs=include_dirs, exclude_dirs=exclude_dirs,
                include_suffixes=include_suffixes, exclude_suffixes=exclude_suffixes,
            )
    except ValueError as e:  # nothing ingestable / bad path -> clean CLI error
        raise click.ClickException(str(e)) from e
    _echo_ingest_report(report, background_failures=bg["failed"])

    url = f"http://{host}:{port}"
    click.echo(f"Serving graph viewer on {url}  (graph: {graph_name})")
    # Launch the browser just before app.run() blocks; the SPA fetches once the server is
    # up (a fraction of a second later) -- a refresh covers the rare bind race.
    if not no_open:
        click.launch(url)
    create_app(client, host).run(host=host, port=port)


@cli.command("enrich")
@click.argument("path")
@click.option("--only", "only", multiple=True,
              help="Run only the named enrichment(s), repeatable (e.g. --only grpc-contract). Default: all registered.")
@click.option("--skip", "skip", multiple=True,
              help="Skip the named enrichment(s), repeatable. Applied after --only.")
@click.option("--host", default=None, help="FalkorDB host. Default: the configured backend (local starts on demand).")
@click.option("--port", default=None, type=int, help="FalkorDB port (only with --host).")
@click.option("--username", default=None, help="FalkorDB username, if auth is enabled.")
@click.option("--password", default=None, help="FalkorDB password, if auth is enabled.")
@click.option("--ssl", is_flag=True, default=False, help="Connect to FalkorDB over TLS.")
@click.option("--graph-name", "graph_name", default=None,
              help="Target graph. Default: the per-directory name (same as `ingest dir` for this PATH).")
def enrich(path: str, only: tuple[str, ...], skip: tuple[str, ...], host: str | None, port: int | None,
           username: str | None, password: str | None, ssl: bool, graph_name: str | None) -> None:
    """Run post-ingestion enrichment passes over an already-ingested graph.

    Enrichments (e.g. the gRPC/Protobuf contract seam) read any input they need from
    PATH and synthesize derived edges onto the graph the language/DB ingestors already
    populated -- so run this AFTER `run sql`/`run oracle` and `ingest`.

    Unlike the automatic pass at the tail of `ingest`, this reads PATH *unfiltered*: it
    still finds its input (e.g. a `.proto` contract) even when you excluded that
    file's extension from `ingest` (e.g. --exclude-ext .proto). Each enrichment is
    idempotent -- it recomputes its own layer, so re-running is safe."""
    import dataclasses
    from pathlib import Path

    from auryn.core.local_source import LocalDirectorySource, WorkingTreeSource
    from auryn.core.project import default_graph_name, resolve_project_root
    from auryn.core.registry import discover_graph_enrichments

    root = Path(path).expanduser()
    if not root.is_dir():
        raise click.ClickException(f"Not a directory: {root}")

    enrichments = discover_graph_enrichments()
    unknown = [n for n in (*only, *skip) if n not in enrichments]
    if unknown:
        available = ", ".join(sorted(enrichments)) or "(none registered)"
        raise click.ClickException(f"unknown enrichment(s): {', '.join(unknown)}. Available: {available}")
    if only:
        enrichments = {n: enrichments[n] for n in only}
    if skip:
        enrichments = {n: e for n, e in enrichments.items() if n not in set(skip)}
    if not enrichments:
        raise click.ClickException("no enrichments are registered (is the providing plugin installed?)")

    graph = graph_name or default_graph_name(resolve_project_root(path))
    config = dataclasses.replace(_resolve_connection(host, port, username, password, ssl), graph_name=graph)
    writer = GraphWriter(connect(config))

    # Deliberately no file_filter: an enrichment targets a specific known file, so the
    # `ingest` language suffix filter (which may have excluded that file's extension) must
    # not hide it here -- that's the whole reason this is a separate command.
    source = WorkingTreeSource(root) if WorkingTreeSource.is_git_worktree(root) else LocalDirectorySource(root)
    target_sha = source.tree_sha()

    ran_any = False
    for name, enrichment in enrichments.items():
        report = enrichment.run(writer, source, target_sha)
        if report is None:
            click.echo(f"{name}: nothing to do (its input file was not found under {root})")
        else:
            click.echo(f"{name}: {report}")
            ran_any = True
    if not ran_any:
        click.echo("No enrichment produced changes.", err=True)


_HOOK_TEMPLATE = '''"""{name} -- an Auryn post hook.

Runs after ingestion (full AND incremental resync); reads the finished graph and synthesizes
derived nodes/edges. MUST be idempotent: delete your own layer first so re-running is a no-op.
Auto-discovered from ~/.auryn/hooks/ -- no install needed. Toggle with `auryn hooks`.
"""
from auryn.core.graph_model import GraphEdge, GraphNode  # noqa: F401

NAME = "{name}"     # the hook's registered name (what `auryn hooks enable/disable` takes)
# ORDER = 100       # optional; hooks run in ascending order (default 100)


def run(writer, repo, target_sha):
    # 1) clear this hook's own layer (idempotency), e.g.:
    #    writer.query("MATCH ()-[r:MY_EDGE]->() DELETE r")
    # 2) read what the language ingestors produced, e.g.:
    #    rows = writer.ro_query("MATCH (f:Function) RETURN f.id").result_set
    # 3) synthesize + merge, e.g.:
    #    writer.merge_edges([GraphEdge("MY_EDGE", "Function", {{"id": a}},
    #                                  "Function", {{"id": b}}) for a, b in ...])
    return {{"note": "TODO: implement {name}"}}
'''


@click.group("hooks")
def hooks_group() -> None:
    """List and toggle post-ingestion hooks (graph enrichments).

    Hooks run after every ingest AND every incremental resync. Author one as a drop-in file with
    `auryn hooks new` (lands in ~/.auryn/hooks/), or ship one as an `auryn.enrichments`
    plugin. Disabling persists to config.toml -- install-wide, or per-repo with --repo."""


@hooks_group.command("list")
@click.option("--repo", "repo", default=None, help="Show enabled/disabled as it applies to this repo's working dir.")
def hooks_list(repo: str | None) -> None:
    """Show registered post hooks: name, phase, run order, source (plugin|local), and state."""
    from auryn.core.app_config import effective_disabled_hooks
    from auryn.core.project import resolve_project_root
    from auryn.core.registry import discover_graph_enrichments, local_hook_errors

    hooks = discover_graph_enrichments()
    local_dir = str(resolve_project_root(repo)) if repo else None
    disabled = effective_disabled_hooks(local_dir)
    errors = local_hook_errors()
    if not hooks and not errors:
        click.echo("No hooks registered. Author one with: auryn hooks new <name>")
        return
    width = max((len(n) for n in hooks), default=4)
    for name, h in sorted(hooks.items(), key=lambda kv: (kv[1].order, kv[0])):
        state = "disabled" if name in disabled else "enabled"
        click.echo(f"  {name:<{width}}  {h.phase:<4}  order={h.order:<5} {h.source:<6}  {state}")
    for stem, err in sorted(errors.items()):
        click.echo(f"  {stem}: BROKEN -- {err}", err=True)


@hooks_group.command("new")
@click.argument("name")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing hook file of the same name.")
def hooks_new(name: str, force: bool) -> None:
    """Scaffold a new local hook at ~/.auryn/hooks/<name>.py from a starter template."""
    from auryn.core.home import hooks_dir

    stem = name.replace("-", "_")
    d = hooks_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stem}.py"
    if path.exists() and not force:
        raise click.ClickException(f"{path} already exists (use --force to overwrite)")
    path.write_text(_HOOK_TEMPLATE.format(name=name))
    click.echo(f"Created {path} (hook name: {name!r})\nEdit run(), then verify with: auryn hooks list")


@hooks_group.command("disable")
@click.argument("name")
@click.option("--repo", "repo", default=None, help="Scope to this repo's working dir; default: install-wide.")
def hooks_disable(name: str, repo: str | None) -> None:
    """Stop a hook from running, on every ingest/resync (install-wide, or for --repo only)."""
    _toggle_hook(name, disabled=True, repo=repo)


@hooks_group.command("enable")
@click.argument("name")
@click.option("--repo", "repo", default=None, help="Scope to this repo's working dir; default: install-wide.")
def hooks_enable(name: str, repo: str | None) -> None:
    """Re-enable a previously disabled hook (install-wide, or for --repo only)."""
    _toggle_hook(name, disabled=False, repo=repo)


def _toggle_hook(name: str, *, disabled: bool, repo: str | None) -> None:
    from auryn.core.app_config import set_hook_disabled
    from auryn.core.project import resolve_project_root
    from auryn.core.registry import discover_graph_enrichments

    # Validate on disable (typo-catch); on enable, allow removing a stale name whose plugin is
    # no longer installed.
    if disabled and name not in discover_graph_enrichments():
        avail = ", ".join(sorted(discover_graph_enrichments())) or "(none registered)"
        raise click.ClickException(f"unknown hook {name!r}. Available: {avail}")
    local_dir = str(resolve_project_root(repo)) if repo else None
    set_hook_disabled(name, disabled, local_dir=local_dir)
    scope = f"repo {local_dir}" if local_dir else "install-wide"
    click.echo(f"{'Disabled' if disabled else 'Enabled'} hook {name!r} ({scope}).")


_attach_ingestor_commands()
cli.add_command(run)
cli.add_command(git_sync)
cli.add_command(ingest_group)
cli.add_command(hooks_group)
