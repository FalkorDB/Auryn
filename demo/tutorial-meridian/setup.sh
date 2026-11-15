#!/usr/bin/env bash
# Reproduce the tutorial graph: stage editable git copies of the four repos and ingest them
# into ONE shared graph, so cross-repo + cross-language edges (the HTTP seam) resolve.
#
#   bash demo/tutorial-meridian/setup.sh
#
# State is isolated under AURYN_HOME so it never touches your own code graph.
set -euo pipefail

export AURYN_HOME="${AURYN_HOME:-/tmp/cg-tutorial}"
WORK="${WORK:-/tmp/cg-tutorial-work}"
SRC="$(cd "$(dirname "$0")/repos" && pwd)"
GRAPH="${GRAPH:-meridian}"

echo "AURYN_HOME=$AURYN_HOME   WORK=$WORK   graph=$GRAPH"
rm -rf "$AURYN_HOME" "$WORK"
mkdir -p "$WORK"

# Stage each repo as its own git repo (so incremental `sync` / the commit hook work).
for r in web-portal api-gateway platform-core platform-db; do
  cp -r "$SRC/$r" "$WORK/$r"
  git -C "$WORK/$r" init -q -b main
  git -C "$WORK/$r" add -A
  git -C "$WORK/$r" -c user.email=demo@meridian.dev -c user.name=demo commit -q -m "init $r"
done

# Ingest all four into the SAME graph. Order-independent; the HTTP seam heals either way.
# --deep off = fast structural pass (enough for the whole tutorial).
for r in platform-core api-gateway web-portal platform-db; do
  uv run auryn ingest --graph-name "$GRAPH" dir "$WORK/$r" --deep off
done

echo
echo "Done. Ask the graph a question:"
echo "  export AURYN_HOME=$AURYN_HOME"
echo "  uv run python demo/tutorial-meridian/ask.py auryn_status '{}'"
echo "  uv run python demo/tutorial-meridian/ask.py trace_entrypoint '{\"entry_id\":\"extern://http/POST:/api/orders\"}'"
echo "Or browse it:  uv run auryn view    # pick the '$GRAPH' graph"
