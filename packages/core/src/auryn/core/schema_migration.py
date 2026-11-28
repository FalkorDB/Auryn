"""Graph-schema-version migration (#851).

A breaking change to the graph's label / relationship-type vocabulary (a bump of
graph_model.GRAPH_SCHEMA_VERSION -- a rename/split/removal, e.g. #849's Typedef->TypeAlias) makes an
already-ingested graph incompatible: an *incremental* resync re-emits only changed files, so nodes
written under the old label linger and queries silently return a half-migrated graph.

Detection is PURELY a version comparison -- the running Auryn stamps the generation it ingests under
in `app_config.Config.schema_version`; when that trails `GRAPH_SCHEMA_VERSION`, the machine last
ingested under an incompatible Auryn. The graph's own labels/edges are never inspected (that would be
fragile -- a graph can lack a renamed label yet still be stale).

What happens on a stale machine, the first time any ingest/sync runs:
  * LOCAL backend (bundled falkordb-lite): delete each registered graph and re-ingest every
    registered repo from scratch -- a clean rebuild under the new vocabulary. Disk hooks and the
    registry survive (they are files/config, not graph nodes; a re-ingest re-wires triggers
    idempotently). This is the "force a single re-ingest" the user asked for.
  * REMOTE / shared backend (any --host, or a remote backend): NEVER destroy someone else's DB. Warn
    that a manual re-ingest is needed, and do NOT advance the (local-graph) stamp.

The stamp is advanced only after a FULLY successful local rebuild, so a failed/partial migration
retries next time rather than marking an incomplete graph as done. A machine-wide inter-process lock
serializes the destructive rebuild across the burst of hook-triggered `sync` processes an upgrade can
spawn, and the version is re-checked after the lock is taken (another process may have just migrated).
"""

from __future__ import annotations

import contextlib
import logging
import threading

from auryn.core.app_config import load_config, set_schema_version
from auryn.core.graph_model import GRAPH_SCHEMA_VERSION

try:
    import fcntl
except ImportError:  # native Windows -- no fcntl; the migration runs unserialized there, as sync does
    fcntl = None

logger = logging.getLogger("auryn.ingest")

# Re-entry guard (in-process): the re-ingests a migration launches must not themselves trigger one.
_state = threading.local()
_remote_warned = False  # warn about an un-migratable remote graph once per process, not every command


@contextlib.contextmanager
def _migration_lock():
    """Machine-wide exclusive lock so a burst of hook-triggered `sync` processes after an upgrade
    don't each delete-and-rebuild concurrently. Blocking (waits for the holder); best-effort where
    fcntl is unavailable (Windows)."""
    if fcntl is None:
        yield
        return
    from auryn.core.home import run_dir

    run_dir().mkdir(parents=True, exist_ok=True)
    lock_path = run_dir() / "schema-migration.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def migrate_schema_if_stale(*, is_local: bool, local_migrate, current_version: int = GRAPH_SCHEMA_VERSION) -> bool:
    """Bring this machine up to date if it last ingested under an older, schema-incompatible Auryn.
    No-op (returns False) once the stored version is current, or while a migration is already running.

    `is_local` is whether the DB this command targets is the bundled local one we may safely destroy
    (the caller decides: default/no `--host` AND a local backend; any explicit `--host` is remote).
    `local_migrate(repos) -> bool` performs the destructive local rebuild and returns True iff EVERY
    graph was rebuilt cleanly; it is injected so this module stays connection-agnostic and testable.
    Returns True iff a full local migration completed (and the stamp advanced)."""
    global _remote_warned
    if getattr(_state, "active", False):
        return False
    cfg = load_config()
    if cfg.schema_version >= current_version:
        return False

    if not is_local:
        # A shared DB is never auto-destroyed; and we must NOT advance the (local-graph) stamp from a
        # remote-targeting command, or the next LOCAL command would skip its own needed migration.
        if not _remote_warned:
            _remote_warned = True
            logger.warning(
                "Auryn's graph schema changed (this machine is stamped v%d, Auryn is now v%d), but this "
                "targets a REMOTE/shared DB -- NOT migrating it (a shared DB is never destroyed "
                "automatically). Re-ingest your graph(s) to adopt the new schema; until then incremental "
                "resyncs may leave stale nodes and queries can be incomplete.",
                cfg.schema_version, current_version,
            )
        return False

    if not cfg.repos:
        set_schema_version(current_version)  # nothing ingested on this machine yet -> just adopt it
        return False

    with _migration_lock():
        cfg = load_config()  # re-check under the lock: another process may have just migrated
        if cfg.schema_version >= current_version:
            return False
        _state.active = True
        try:
            logger.warning(
                "Auryn's graph schema changed (v%d -> v%d). Re-ingesting %d local repo(s) from scratch so "
                "the graph matches the new schema (one-time migration; hooks and registry are kept).",
                cfg.schema_version, current_version, len(cfg.repos),
            )
            fully_migrated = bool(local_migrate(cfg.repos))
            if fully_migrated:
                set_schema_version(current_version)  # advance ONLY after a clean, complete rebuild
            else:
                logger.warning(
                    "Schema migration did not complete for every repo -- NOT advancing the stored version; "
                    "it will retry on the next ingest/sync. Fix the reported repo(s) or `auryn forget` a dead one."
                )
            return fully_migrated
        finally:
            _state.active = False
