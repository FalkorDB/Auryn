# Sharing a code graph (portability)

A FalkorDB graph **is** a Redis RDB file, so sharing a code graph is a file copy — no bespoke
exporter, and nothing is lost. The RDB carries the *entire* dataset: every graph on the instance,
its vector and full-text indexes, embeddings, and all node/edge properties. The recipient loads it
instantly, with **no re-ingest**.

Where the RDB lives:

- **Local / embedded backend** — `~/.auryn/store.db` (or `$AURYN_HOME/store.db`).
- **Remote backend** — the server's `dump.rdb`, written on `SAVE` / `BGSAVE` (and on shutdown).

## Export

**Local backend** — flush and copy:

```bash
# From a client, flush the in-memory graph to the RDB, then copy it:
redis-cli -p <port> SAVE           # or let `auryn stop` flush on shutdown
cp ~/.auryn/store.db  my-project.rdb
```

**Remote backend** — snapshot on the server and copy its dump:

```bash
redis-cli -h <host> -p <port> BGSAVE
cp /path/to/server/dump.rdb  my-project.rdb
```

**Programmatically** (`auryn.core.portability`):

```python
from auryn.core.portability import export_graph
export_graph(client, "my-project.rdb")   # issues SAVE, copies the backend's RDB
```

## Import

The backend must be **stopped** first — a running daemon holds the graph in memory and would
overwrite the file on its next save:

```bash
auryn stop
cp my-project.rdb  ~/.auryn/store.db     # or into a fresh $AURYN_HOME
auryn list                                  # any command auto-starts the daemon on the new RDB
```

Or programmatically:

```python
from auryn.core.portability import import_graph
import_graph("my-project.rdb", "~/.auryn/store.db")   # validates it's an RDB, then copies
```

## Caveats

- **Needs a backend to consume.** Unlike a plain JSON export, an RDB is loaded by FalkorDB (or the
  bundled embedded backend) — not opened as a standalone file. In return you get the full graph
  (indexes + embeddings), not just nodes/edges.
- **Binary, not text-diffable.** Great for sharing/transport and committable to git, but you won't
  see a meaningful diff in a PR.
- **Version-sensitive.** The RDB format is tied to the FalkorDB/redis version; export and import
  with compatible versions.

> Verified: copying a built `store.db` into a fresh `AURYN_HOME` and running `auryn list`
> shows every graph with its node/edge counts — no re-ingest.
