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
  * REMOTE / shared backend: NEVER destroy someone else's DB. Warn that a manual re-ingest is needed.
Either way the stored version is bumped afterward, so this runs exactly once per upgrade.
"""

from __future__ import annotations

import logging
import threading

from auryn.core.app_config import load_config, set_schema_version
from auryn.core.graph_model import GRAPH_SCHEMA_VERSION

logger = logging.getLogger("auryn.ingest")

# Re-entry guard: the re-ingests a migration launches must not themselves trigger a migration. Thread-
# local so a long-lived server migrating on one thread doesn't suppress an unrelated thread's check.
_state = threading.local()


def migrate_schema_if_stale(*, is_local: bool, local_migrate, current_version: int = GRAPH_SCHEMA_VERSION) -> bool:
    """Bring this machine up to date if it last ingested under an older, schema-incompatible Auryn.
    No-op (returns False) once the stored version is current, or while a migration is already running.

    `is_local` is whether the DB this command targets is the bundled local one we may safely destroy
    (the caller decides: default/no `--host` AND a local backend; any explicit `--host` is treated as
    remote/shared). `local_migrate(repos)` performs the destructive local rebuild (delete each graph +
    re-ingest every registered repo) -- injected so this module stays connection-agnostic and
    unit-testable. It runs only when `is_local` and there are registered repos; a remote/shared backend
    is only warned about and never touched. Returns True iff `local_migrate` actually ran."""
    if getattr(_state, "active", False):
        return False
    cfg = load_config()
    if cfg.schema_version >= current_version:
        return False

    if not is_local:
        logger.warning(
            "Auryn's graph schema changed (this machine is stamped v%d, Auryn is now v%d), but this "
            "targets a REMOTE/shared DB -- NOT migrating it (a shared DB is never destroyed "
            "automatically). Re-ingest your graph(s) to adopt the new schema; until then incremental "
            "resyncs may leave stale nodes and queries can be incomplete.",
            cfg.schema_version, current_version,
        )
        set_schema_version(current_version)  # warned once; don't nag on every ingest
        return False

    if not cfg.repos:
        set_schema_version(current_version)  # nothing ingested on this machine yet -> just adopt it
        return False

    _state.active = True
    try:
        logger.warning(
            "Auryn's graph schema changed (v%d -> v%d). Re-ingesting %d local repo(s) from scratch so "
            "the graph matches the new schema (one-time migration; hooks and registry are kept).",
            cfg.schema_version, current_version, len(cfg.repos),
        )
        local_migrate(cfg.repos)
        set_schema_version(current_version)
        return True
    finally:
        _state.active = False
