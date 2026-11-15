"""Share a code graph by its RDB file.

A FalkorDB graph *is* a Redis RDB (the local/embedded backend persists to ``store.db``; a remote
server writes ``dump.rdb`` on SAVE/BGSAVE). So making a graph portable is not a bespoke exporter --
it's a flush + file copy. Unlike a JSON/GraphML export, the RDB carries the **whole** graph: vector
and full-text indexes, embeddings, and every node/edge property, and it loads instantly with no
re-ingest (verified: copy a ``store.db`` into a fresh ``AURYN_HOME`` and ``auryn list`` shows
every graph).

Caveats worth stating to a user:
- Consuming an RDB needs a FalkorDB (or the bundled embedded) backend -- it is not zero-service the
  way Graphify's plain ``graph.json`` is.
- The RDB is binary: shareable/committable, but not text-diffable in a PR.
- RDB format is FalkorDB/redis-version sensitive; export and import with compatible versions.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_RDB_MAGIC = b"REDIS"


def _config_get(conn, key: str) -> str:
    """One CONFIG GET value, tolerant of the two client shapes: redis-py parses CONFIG GET into a
    ``{key: value}`` dict; a raw client returns a flat ``[key, value]`` list."""
    res = conn.execute_command("CONFIG", "GET", key)
    if isinstance(res, dict):
        return res[key]
    return res[1]


def _rdb_path_of(client) -> Path:
    """The RDB file the connected backend persists to, from ``CONFIG GET dir``/``dbfilename`` --
    so this works for the embedded backend and any local server without hard-coding a path."""
    conn = client.connection
    return Path(_config_get(conn, "dir")) / _config_get(conn, "dbfilename")


def export_graph(client, dest: str | Path) -> Path:
    """Snapshot the connected backend's graph(s) to a portable RDB at ``dest``.

    Issues ``SAVE`` (flush the in-memory graph to the backend's RDB) then copies the RDB file. The
    copy is the entire dataset -- every graph on the instance, with indexes and embeddings intact.
    Works when the RDB is on the local filesystem (the embedded/local backend); for a remote server
    whose filesystem you can't reach, run ``BGSAVE`` on it and copy its ``dump.rdb`` directly."""
    conn = client.connection
    conn.execute_command("SAVE")  # flush the in-memory graph to the RDB before copying
    src = _rdb_path_of(client)
    if not src.exists():
        raise FileNotFoundError(
            f"backend RDB not found at {src} -- the graph's RDB isn't on this machine "
            f"(a remote server?). Run BGSAVE on the server and copy its dump.rdb instead."
        )
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def import_graph(src: str | Path, db_path: str | Path) -> Path:
    """Install a portable RDB (from :func:`export_graph`, a shared ``store.db``, or a server's
    ``dump.rdb``) as the backend's RDB at ``db_path``, so the next backend start serves it.

    The backend MUST be stopped first: a running daemon holds the graph in memory and would
    overwrite ``db_path`` on its next save. The usual flow is ``auryn stop`` -> import -> run any
    command (which auto-starts the daemon on the imported RDB). Validates the source is an RDB."""
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    with src.open("rb") as fh:
        if fh.read(len(_RDB_MAGIC)) != _RDB_MAGIC:
            raise ValueError(f"{src} is not a Redis RDB file (bad magic); refusing to import it")
    dst = Path(db_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst
