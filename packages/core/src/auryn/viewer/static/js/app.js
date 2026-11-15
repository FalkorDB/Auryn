// Bootstrap + wiring. Owns app state and the DOM element references; delegates
// rendering to render.js (the WebGL view) and panels.js (the floating panels).

import { Api } from "./api.js";
import { Palette } from "./palette.js";
import { GraphView } from "./render.js";
import * as P from "./panels.js";

const $ = (id) => document.getElementById(id);
const LAST_GRAPH_KEY = "auryn.lastGraph";

const state = { graphName: null, semantic: false };
const prefs = { captions: true, edgeLabels: false };
const palette = new Palette();
let overviewCounts = {}; // label -> count, graph-wide (from overview)
let onCanvasCounts = {}; // label -> count, currently loaded on the canvas

const refs = {
  veil: $("veil"), veilHint: $("veil-hint"),
  picker: $("graph-picker"),
  searchInput: $("search-input"), searchScope: $("search-scope"), results: $("results"),
  searchMode: $("search-mode"),
  cats: $("cats"), catsEmpty: $("cats-empty"),
  rels: $("rels"), relsEmpty: $("rels-empty"),
  gitList: $("git-list"), gitEmpty: $("git-empty"),
  inspector: {
    root: $("inspector"), badge: $("insp-badge"), name: $("insp-name"),
    actions: $("insp-actions"), body: $("insp-body"),
  },
  statNodes: $("stat-nodes"), statEdges: $("stat-edges"), statNote: $("stat-note"),
  status: $("status"), controls: $("controls"), toast: $("toast"),
};

// ---- toast ----------------------------------------------------------------
let toastTimer = null;
function toast(msg, isErr = false) {
  refs.toast.textContent = msg;
  refs.toast.classList.toggle("err", isErr);
  refs.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => refs.toast.classList.remove("show"), 3800);
}

// ---- the view -------------------------------------------------------------
const view = new GraphView($("graph"), {
  palette, prefs,
  // left-click only highlights; the inspector opens on right-click (onInspect /
  // onInspectEdge). A left-click on empty space (id null) closes the panel.
  onSelect: (id) => { if (!id) P.closeInspector(refs.inspector); },
  onInspect: (id) => openInspector(id),
  onInspectEdge: (key) => openEdgeInspector(key),
  onExpand: (id) => expandNode(id),
  onCountsChanged: (nodes, edges, catCounts) => {
    refs.statNodes.textContent = nodes.toLocaleString();
    refs.statEdges.textContent = edges.toLocaleString();
    onCanvasCounts = catCounts || {};
    P.updateCategoryCounts(refs.cats, onCanvasCounts);
    setVeil(nodes === 0);
    if (nodes > 0) scheduleCommunityRecolor(); // keep community colours current as the view grows
  },
  onSelectionChanged: (nodeCount, edgeCount) => updateSelbar(nodeCount, edgeCount),
});

// recolour the graph live whenever the palette changes (colour or visibility)
palette.onChange(() => view.repaint());

// legend callback: always repaint; on a visibility change also reframe + refresh
// the empty-state hint (a colour change shouldn't move the camera).
function legendRepaint(reframe) {
  view.repaint();
  if (reframe) refreshEmptyState();
}

// expose for debugging / automation
window.__gv = view;

// ---- veil -----------------------------------------------------------------
function setVeil(show, hint) {
  refs.veil.classList.toggle("hidden", !show);
  if (hint) refs.veilHint.textContent = hint;
}

// ---- inspector ------------------------------------------------------------
const gitCtx = {
  api: Api,
  getGraphName: () => state.graphName,
  onSynced: () => reloadGraph(),
  toast,
};

function openInspector(id) {
  let node = view.getNode(id);
  if (!node) return;
  P.renderInspector(refs.inspector, node, palette, {
    expand: (nid) => expandNode(nid),
    focus: (nid) => view.focus(nid),
    hide: (nid) => { view.hideNode(nid); P.closeInspector(refs.inspector); },
    pathFrom: (n) => seedPathEnd("from", n),
    pathTo: (n) => seedPathEnd("to", n),
  }, (project) => P.buildGitCard(project, gitCtx));
}

function openEdgeInspector(key) {
  const edge = view.getEdge(key);
  if (!edge) return;
  P.renderEdgeInspector(refs.inspector, edge, view.getNode(edge.from), view.getNode(edge.to), palette, {
    select: (nid) => { if (view.hasNode(nid)) { view.select(nid); openInspector(nid); view.focus(nid); } },
  });
}

// After a visibility/filter change: reframe the visible nodes and, if everything
// is filtered out, tell the user how to get it back (rather than a blank canvas).
function refreshEmptyState() {
  if (!state.graphName || view.order() === 0) return;
  const visible = view.visibleCount();
  if (visible === 0) {
    setVeil(true, 'Everything is hidden — use "Show all" to bring the categories back.');
  } else {
    setVeil(false);
    view.fit(400);
  }
}

// ---- graph loading --------------------------------------------------------
async function loadGraphList() {
  let graphs = [];
  try { graphs = await Api.listGraphs(); } catch (e) { toast(`Couldn't list graphs: ${e.message}`, true); }
  refs.picker.innerHTML =
    `<option value="">No graph</option>` +
    graphs.map((g) => `<option value="${P.esc(g)}">${P.esc(g)}</option>`).join("");
  const last = localStorage.getItem(LAST_GRAPH_KEY);
  const pick = graphs.includes(last) ? last : graphs.length === 1 ? graphs[0] : "";
  if (pick) { refs.picker.value = pick; selectGraph(pick); }
}

async function selectGraph(name) {
  state.graphName = name || null;
  view.clear();
  P.closeInspector(refs.inspector);
  refs.results.innerHTML = "";
  refs.searchInput.value = "";
  refs.statNote.textContent = "";
  resetPathForm();
  resetCommunityColoring();

  if (!name) {
    localStorage.removeItem(LAST_GRAPH_KEY);
    refs.status.hidden = true; refs.controls.hidden = true;
    P.renderCategories(refs.cats, refs.catsEmpty, { nodes: [] }, palette, () => {});
    P.renderRelationships(refs.rels, refs.relsEmpty, { edges: [] });
    P.mountGitProjects(refs.gitList, refs.gitEmpty, gitCtx);
    setVeil(true, "Select a graph to begin");
    return;
  }
  localStorage.setItem(LAST_GRAPH_KEY, name);
  refs.status.hidden = false; refs.controls.hidden = false;
  setVeil(true, "Loading…");

  let overview, roots;
  try {
    [overview, roots] = await Promise.all([Api.overview(name), Api.roots(name)]);
  } catch (e) {
    toast(`Couldn't load graph: ${e.message}`, true);
    setVeil(true, "Failed to load");
    return;
  }

  overviewCounts = Object.fromEntries(overview.nodes.map((n) => [n.label, n.count]));
  palette.load(name, overview.nodes.map((n) => n.label));
  P.renderCategories(refs.cats, refs.catsEmpty, overview, palette, legendRepaint);
  P.renderRelationships(refs.rels, refs.relsEmpty, overview);
  refs.searchScope.innerHTML =
    `<option value="">any</option>` +
    overview.nodes.map((n) => `<option value="${P.esc(n.label)}">${P.esc(n.label)}</option>`).join("");
  P.mountGitProjects(refs.gitList, refs.gitEmpty, gitCtx);

  if (!roots.length) {
    setVeil(true, "This graph has no root entry points — search to explore.");
    return;
  }
  // Gather a connected seed FIRST, then render it in one shot — so the graph
  // appears already laid out and framed, with no incremental redraws or shifting.
  const seed = await gatherSeed(roots);
  view.setData(seed.nodes, seed.edges);
  refs.statNote.textContent = "double-click a node to expand · scroll to zoom";
}

// Root nodes are usually one or two containers (Solution / Project), so a
// roots-only view is a lone dot. BFS a layer at a time from the roots until we
// have a few hundred connected nodes — accumulating into plain arrays without
// touching the view, so nothing renders until the whole seed is ready.
async function gatherSeed(roots) {
  const CAP = 260, MAX_CALLS = 16;
  const byId = new Map();
  const edges = [];
  const edgeSeen = new Set();
  for (const r of roots) byId.set(r.id, r);
  const visited = new Set();
  let frontier = roots.map((r) => r.id);
  let calls = 0;
  while (frontier.length && byId.size < CAP && calls < MAX_CALLS) {
    const next = [];
    for (const id of frontier) {
      if (visited.has(id)) continue;
      visited.add(id);
      if (byId.size >= CAP || calls >= MAX_CALLS) break;
      try {
        const d = await Api.neighbors(state.graphName, id);
        calls++;
        for (const n of [d.center, ...d.nodes]) if (!byId.has(n.id)) byId.set(n.id, n);
        for (const e of d.edges) {
          const k = `${e.from}|${e.type}|${e.to}`;
          if (!edgeSeen.has(k)) { edgeSeen.add(k); edges.push(e); }
        }
        for (const n of d.nodes) if (!visited.has(n.id)) next.push(n.id);
      } catch { /* skip a node that failed to expand */ }
    }
    frontier = next;
  }
  return { nodes: [...byId.values()], edges };
}

async function reloadGraph() {
  if (state.graphName) await selectGraph(state.graphName);
}

// Highlight a just-surfaced set of entities as the amber selection — the single,
// consistent way every add-to-canvas action (query, expand) shows "here's what
// this brought in". `fit` frames them (for results that may be off-screen); it's
// left off for local adds like expansion, where the camera shouldn't jump.
function highlightAdded(nodes = [], edges = [], { fit = false } = {}) {
  const nodeIds = nodes.map((n) => n.id);
  const edgeKeys = edges.map((e) => `${e.from}|${e.type}|${e.to}`);
  view.setSelection(nodeIds, edgeKeys, { fit });
}

async function expandNode(id) {
  if (!state.graphName) return;
  try {
    const data = await Api.neighbors(state.graphName, id);
    if (!view.hasNode(id)) view.merge([data.center], [], { layout: false });
    const added = view.merge(data.nodes, data.edges, { spawn: id, animate: true });
    // Same highlight a query result gets (category colours, rest greyed), so the
    // new neighbours are obvious; no reframe — they fan out next to the source.
    highlightAdded(data.nodes, data.edges);
    if (data.truncated) toast("Node has more neighbours than shown — some were truncated.");
    else if (added === 0) toast("No new neighbours to add.");
  } catch (e) {
    toast(`Expand failed: ${e.message}`, true);
  }
}

async function expandAll() {
  if (!state.graphName) return;
  const btn = $("ctl-expand");
  btn.classList.add("busy");
  const prev = btn.textContent;
  btn.textContent = "Loading…";
  try {
    const selNodes = view.getSelection().nodes;
    if (selNodes.length) {
      // Respect the selection: expand EVERY selected entity's neighbours (all of
      // them — no per-node/selection cap, unlike the toolbar's "Expand"), and
      // leave the rest of the graph untouched. The selection plus its new
      // neighbours end up highlighted.
      const allNodes = [], allEdges = [];
      for (const id of selNodes) {
        try {
          const d = await Api.neighbors(state.graphName, id);
          allNodes.push(...d.nodes); allEdges.push(...d.edges);
        } catch { /* skip a node that failed to expand */ }
      }
      const added = view.merge(allNodes, allEdges, { animate: true });
      const selObjs = selNodes.map((id) => view.getNode(id)).filter(Boolean);
      highlightAdded([...selObjs, ...allNodes], allEdges); // selection ∪ neighbours
      refs.statNote.textContent = "";
      refs.statNote.className = "note";
      toast(`Expanded ${selNodes.length} selected node${selNodes.length === 1 ? "" : "s"} · +${added} new`);
    } else {
      // Nothing selected → load the whole graph, as before.
      const data = await Api.all(state.graphName, 2000);
      setVeil(data.nodes.length === 0);
      view.setData(data.nodes, data.edges);
      refs.statNote.textContent = data.truncated
        ? "showing a capped slice — graph has more nodes"
        : "";
      refs.statNote.className = data.truncated ? "note warn" : "note";
    }
  } catch (e) {
    toast(`Expand-all failed: ${e.message}`, true);
  } finally {
    btn.classList.remove("busy");
    btn.textContent = prev;
  }
}

// ---- cypher query ---------------------------------------------------------
async function runCypher() {
  if (!state.graphName) return;
  const cypher = $("query-input").value.trim();
  const status = $("query-status"), btn = $("query-run");
  if (!cypher) { status.textContent = "Enter a query."; status.className = "query-status"; return; }
  btn.disabled = true;
  status.textContent = "Running…";
  status.className = "query-status";
  try {
    const data = await Api.runQuery(state.graphName, cypher);
    if (!data.node_count && !data.edge_count) {
      status.textContent = `${data.row_count} row(s) — no nodes or relationships returned.`;
      status.className = "query-status";
    } else {
      const added = view.merge(data.nodes, data.edges, { animate: true });
      setVeil(view.order() === 0);
      // Highlight (and frame) exactly what the query returned, so the result
      // entities stand out amber against whatever else is on the canvas rather
      // than silently blending in — the same treatment expansion gives.
      highlightAdded(data.nodes, data.edges, { fit: true });
      status.textContent =
        `${data.node_count} nodes, ${data.edge_count} edges (+${added} new) · highlighted` +
        (data.truncated ? " · capped" : "");
      status.className = "query-status ok";
    }
  } catch (e) {
    status.textContent = e.message;
    status.className = "query-status err";
  } finally {
    btn.disabled = false;
  }
}

// ---- search ---------------------------------------------------------------
// The unlabelled search scans every node name in the DB, so responses can be
// slow and arrive out of order. `searchSeq` tags each request and only the most
// recent one is allowed to render — otherwise a slow older response could
// clobber the results of a newer query (or a cleared box), which read as search
// "randomly showing nothing".
let searchTimer = null;
let searchSeq = 0;
function onSearchInput() {
  clearTimeout(searchTimer);
  const q = refs.searchInput.value.trim();
  const mySeq = ++searchSeq;
  if (!q || !state.graphName) { refs.results.innerHTML = ""; return; }
  // Semantic search embeds the query server-side (a model call), so it's costlier than the
  // name scan -> debounce a little longer and query by meaning.
  const semantic = state.semantic;
  searchTimer = setTimeout(async () => {
    try {
      const results = semantic
        ? await Api.searchSemantic(state.graphName, { q, k: 30 })
        : await Api.search(state.graphName, { label: refs.searchScope.value || null, q, limit: 30 });
      if (mySeq !== searchSeq) return; // superseded by a newer query
      P.renderSearchResults(refs.results, results, palette, pickResult);
    } catch (e) {
      if (mySeq !== searchSeq) return;
      P.renderSearchResults(refs.results, [], palette, pickResult);
      toast(`${semantic ? "Semantic search" : "Search"} failed: ${e.message}`, true);
    }
  }, semantic ? 350 : 200);
}

// Toggle name <-> semantic search. In semantic mode the category filter doesn't apply
// (results are ranked by meaning across all kinds), so it's disabled; the placeholder flips
// to cue the change, and any current query is re-run in the new mode.
function toggleSearchMode() {
  state.semantic = !state.semantic;
  refs.searchMode.setAttribute("aria-pressed", String(state.semantic));
  refs.searchInput.placeholder = state.semantic ? "Search by meaning…" : "Search entities…";
  refs.searchScope.disabled = state.semantic;
  onSearchInput();
  refs.searchInput.focus();
}

async function pickResult(n) {
  refs.results.innerHTML = "";
  refs.searchInput.value = n.name;
  try {
    const data = await Api.neighbors(state.graphName, n.id);
    view.merge([data.center, ...data.nodes], data.edges, { spawn: null, animate: true });
    setVeil(false);
    view.select(n.id);
    openInspector(n.id);
    setTimeout(() => view.focus(n.id), 60);
  } catch (e) {
    // fall back to just placing the node
    view.merge([n], []);
    view.select(n.id);
    openInspector(n.id);
    toast(`Couldn't load neighbours: ${e.message}`, true);
  }
}

// ---- tabs -----------------------------------------------------------------
function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.setAttribute("aria-selected", String(t.dataset.tab === name)));
  document.querySelectorAll(".tabpage").forEach((p) =>
    p.setAttribute("data-active", String(p.dataset.tab === name)));
}

// ---- path finder ----------------------------------------------------------
// Two endpoint pickers (each its own search-as-you-type) feed the /path API;
// the result is a shortest connection traced green on the canvas plus a chain
// breadcrumb in the panel. Endpoints can also be seeded from the inspector
// ("Path from" / "Path to"), which is the natural way to pick from the canvas.
const pathEnds = { from: null, to: null };   // picked node {id,name,label,...} per side
const pathSeq = { from: 0, to: 0 };          // per-side request counter (see onSearchInput)
const pathTimer = { from: null, to: null };

function pathEls(kind) {
  return {
    input: $(`path-${kind}`),
    results: $(`path-${kind}-results`),
    clear: $(`path-${kind}-clear`),
    dot: $(`path-${kind}-dot`),
  };
}

function setPathStatus(msg, kind = "") {
  const s = $("path-status");
  s.textContent = msg;
  s.className = "query-status" + (kind ? " " + kind : "");
}

// Lock in (or clear, with node=null) one endpoint. keepInput leaves whatever the
// user is typing in place — used when an edit invalidates a previously picked end.
function setPathEnd(kind, node, { keepInput = false } = {}) {
  pathEnds[kind] = node || null;
  const els = pathEls(kind);
  els.results.innerHTML = "";
  if (node) {
    els.input.value = node.name;
    els.dot.hidden = false;
    els.dot.style.background = palette.colorFor(node.label);
    els.clear.hidden = false;
  } else {
    if (!keepInput) els.input.value = "";
    els.dot.hidden = true;
    els.clear.hidden = true;
  }
  setPathStatus("");
}

// Seed an endpoint from elsewhere (the inspector) and jump to the Path tab.
function seedPathEnd(kind, node) {
  setPathEnd(kind, node);
  activateTab("path");
  toast(`Path ${kind === "from" ? "start" : "end"}: ${node.name}`);
}

function onPathSearch(kind) {
  const els = pathEls(kind);
  clearTimeout(pathTimer[kind]);
  const q = els.input.value.trim();
  const mySeq = ++pathSeq[kind];
  // editing the text after a pick means the user is re-choosing — drop the lock
  if (pathEnds[kind] && q !== pathEnds[kind].name) setPathEnd(kind, null, { keepInput: true });
  if (!q || !state.graphName) { els.results.innerHTML = ""; return; }
  pathTimer[kind] = setTimeout(async () => {
    try {
      const results = await Api.search(state.graphName, { q, limit: 20 });
      if (mySeq !== pathSeq[kind]) return; // superseded by a newer keystroke
      P.renderSearchResults(els.results, results, palette, (n) => setPathEnd(kind, n));
    } catch (e) {
      if (mySeq !== pathSeq[kind]) return;
      els.results.innerHTML = "";
      toast(`Search failed: ${e.message}`, true);
    }
  }, 200);
}

function swapPathEnds() {
  const from = pathEnds.from, to = pathEnds.to;
  setPathEnd("from", to);
  setPathEnd("to", from);
}

function clearPathHighlight() {
  view.clearPath();
  $("path-result").innerHTML = "";
  setPathStatus("");
}

function resetPathForm() {
  setPathEnd("from", null);
  setPathEnd("to", null);
  $("path-result").innerHTML = "";
  setPathStatus("");
}

async function findPath() {
  if (!state.graphName) { setPathStatus("Select a graph first.", "err"); return; }
  if (!pathEnds.from || !pathEnds.to) { setPathStatus("Pick a start and an end entity.", "err"); return; }
  if (pathEnds.from.id === pathEnds.to.id) { setPathStatus("Start and end are the same node.", "err"); return; }

  const directed = $("path-directed").checked;
  let depth = parseInt($("path-depth").value, 10);
  if (!Number.isFinite(depth) || depth < 1) depth = 6;
  depth = Math.min(10, depth);
  $("path-depth").value = String(depth);

  const btn = $("path-find");
  btn.disabled = true;
  setPathStatus("Finding…");
  try {
    const data = await Api.path(state.graphName, {
      from: pathEnds.from.id, to: pathEnds.to.id, maxDepth: depth, directed,
    });
    if (!data.found) {
      view.clearPath();
      $("path-result").innerHTML = "";
      setPathStatus(directed
        ? `No directed path within ${depth} hops — try “Any direction” or more hops.`
        : `No path within ${depth} hops — try more hops.`, "err");
      return;
    }
    view.showPath(data.nodes, data.edges);
    setVeil(false);
    P.renderPathResult($("path-result"), data, palette, {
      pick: (n) => { if (view.hasNode(n.id)) { view.select(n.id); openInspector(n.id); view.focus(n.id); } },
    });
    const hops = data.hops;
    setPathStatus(`${hops} hop${hops === 1 ? "" : "s"} · ${data.nodes.length} nodes`, "ok");
  } catch (e) {
    setPathStatus(e.message, "err");
    toast(`Path search failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

// ---- multi-selection & canvas --------------------------------------------
// The floating selection toolbar reflects render.js's selection set (driven by
// shift-click, the ctrl/⌘ lasso, or a query result) and acts on it.
function updateSelbar(nodeCount = 0, edgeCount = 0) {
  const total = nodeCount + edgeCount;
  const bar = $("selbar");
  if (!total) { bar.hidden = true; return; }
  const parts = [];
  if (nodeCount) parts.push(`${nodeCount} node${nodeCount === 1 ? "" : "s"}`);
  if (edgeCount) parts.push(`${edgeCount} edge${edgeCount === 1 ? "" : "s"}`);
  $("sel-count").textContent = `${parts.join(" · ")} selected`;
  $("sel-expand").style.display = nodeCount ? "" : "none"; // nothing to expand if only edges
  bar.hidden = false;
}

// Load the neighbours of every selected node in one batched merge (capped, since
// a huge selection would be thousands of API calls). The selection itself stays.
async function expandSelected() {
  if (!state.graphName) return;
  const { nodes } = view.getSelection();
  if (!nodes.length) return;
  const CAP = 50;
  const targets = nodes.slice(0, CAP);
  const btn = $("sel-expand");
  btn.disabled = true;
  try {
    const allNodes = [], allEdges = [];
    for (const id of targets) {
      try {
        const d = await Api.neighbors(state.graphName, id);
        allNodes.push(...d.nodes); allEdges.push(...d.edges);
      } catch { /* skip a node that failed to expand */ }
    }
    const added = view.merge(allNodes, allEdges, { animate: true });
    // Highlight the surfaced neighbourhood, consistent with query/single-expand.
    highlightAdded(allNodes, allEdges);
    let msg = `Expanded ${targets.length} node${targets.length === 1 ? "" : "s"} · +${added} new`;
    if (nodes.length > CAP) msg += ` (first ${CAP} of ${nodes.length})`;
    toast(msg);
  } finally { btn.disabled = false; }
}

function removeSelected() {
  const { nodes, edges } = view.removeSelection();
  const parts = [];
  if (nodes) parts.push(`${nodes} node${nodes === 1 ? "" : "s"}`);
  if (edges) parts.push(`${edges} edge${edges === 1 ? "" : "s"}`);
  if (parts.length) toast(`Removed ${parts.join(" and ")} from the canvas.`);
  if (view.order() === 0) setVeil(true, "Canvas cleared — run a query, expand all, or pick a search result.");
}

function clearCanvas() {
  view.clear();
  P.closeInspector(refs.inspector);
  setVeil(true, "Canvas cleared — run a query, expand all, or pick a search result.");
}

// True when focus is in a text field, so a global key shortcut (Backspace/Delete)
// must defer to normal text editing instead of acting on the graph.
function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

// ---- colour by community --------------------------------------------------
// Detect communities over the WHOLE graph (deterministic LPA), colour the loaded
// nodes by their community, and label the dominant ones. Names are heuristic
// (dominant source directory) instantly, then upgraded with AI labels when a
// credential is available — cached per community id so expansions reuse them.
const COMMUNITY_LEGEND_MAX = 14;
const llmCommunityNames = new Map(); // community id -> AI label
let communitySeq = 0;
let communityRecolorTimer = null;

function dirOfNode(node) {
  const fp = node.properties && (node.properties.file_path || node.properties.path);
  let p = fp;
  if (!p && typeof node.id === "string") { const m = node.id.match(/\/file\/(.+)$/); if (m) p = m[1]; }
  if (!p) return null;
  const parts = String(p).split("/"); parts.pop();
  return parts.length ? parts.slice(-2).join("/") : null;
}

function groupLoadedByCommunity() {
  const m = new Map();
  for (const id of view.nodeIds()) {
    const c = view.communityOf(id);
    if (c == null) continue;
    const n = view.getNode(id);
    if (!n) continue;
    if (!m.has(c)) m.set(c, []);
    m.get(c).push(n);
  }
  return m;
}

// Heuristic label: the most common source directory among a community's members
// (falls back to a representative member name).
function heuristicCommunityName(members) {
  const counts = new Map();
  for (const n of members) { const d = dirOfNode(n); if (d) counts.set(d, (counts.get(d) || 0) + 1); }
  if (counts.size) return [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0];
  return (members[0] && members[0].name) || "community";
}

function renderCommunityLegend(rows, total, naming) {
  const box = $("community-legend");
  if (!rows) { box.hidden = true; return; }
  box.hidden = false;
  $("community-legend-title").textContent = `Communities · ${total} in view`;
  const list = $("community-list");
  list.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    li.className = "comm-row" + (naming && !llmCommunityNames.has(r.cid) ? " pending" : "");
    li.innerHTML =
      `<span class="swatch" style="background:${r.color}"></span>` +
      `<span class="cname" title="${P.esc(r.name)}">${P.esc(r.name)}</span>` +
      `<span class="ccount">${r.count}</span>`;
    list.appendChild(li);
  }
  $("community-hint").textContent = naming ? "Naming communities with AI…" : "";
}

// Fetch community assignments for the loaded nodes, colour, and build the legend.
async function applyCommunityColoring() {
  if (!state.graphName || view.order() === 0) return;
  const mySeq = ++communitySeq;
  const data = await Api.communities(state.graphName, view.nodeIds());
  // Abort if superseded by a newer call, or the user toggled the mode off while
  // this was in flight. Guard on the checkbox (intent), NOT view.colorMode() —
  // the mode only flips to "community" once setCommunities runs, just below.
  if (mySeq !== communitySeq || !$("pref-communities").checked) return;
  const ranked = view.setCommunities(data.communities); // [[cid, count], ...]
  const groups = groupLoadedByCommunity();
  const rows = ranked.slice(0, COMMUNITY_LEGEND_MAX).map(([cid, count]) => {
    const members = groups.get(cid) || [];
    return {
      cid, count, members, color: view.communityColorFor(cid),
      name: llmCommunityNames.get(cid) || heuristicCommunityName(members),
    };
  });
  const anyUnnamed = rows.some((r) => !llmCommunityNames.has(r.cid));
  renderCommunityLegend(rows, data.community_count, anyUnnamed);
  if (anyUnnamed) upgradeCommunityNames(rows, data.community_count, mySeq);
}

// Ask the backend (Claude) for semantic names; on success replace the legend
// labels, on failure keep the heuristic ones and say why.
async function upgradeCommunityNames(rows, total, seq) {
  const need = rows.filter((r) => !llmCommunityNames.has(r.cid));
  if (!need.length) { renderCommunityLegend(rows, total, false); return; }
  try {
    const payload = need.map((r) => ({
      samples: r.members.slice(0, 15).map((n) => n.name || n.id).filter(Boolean),
      size: r.count,
    }));
    const res = await Api.communityNames(state.graphName, payload);
    if (seq !== communitySeq || view.colorMode() !== "community") return;
    (res.labels || []).forEach((label, i) => { if (label) llmCommunityNames.set(need[i].cid, label); });
    renderCommunityLegend(rows.map((r) => ({ ...r, name: llmCommunityNames.get(r.cid) || r.name })), total, false);
  } catch (e) {
    if (seq !== communitySeq) return;
    renderCommunityLegend(rows, total, false); // keep heuristic names
    $("community-hint").textContent = "AI naming unavailable (set ANTHROPIC_API_KEY) — using directory names.";
  }
}

async function setCommunityColoring(on) {
  if (!on) { communitySeq++; view.setCategoryColoring(); renderCommunityLegend(null); return; }
  if (!state.graphName || view.order() === 0) { $("pref-communities").checked = false; return; }
  try {
    await applyCommunityColoring();
  } catch (e) {
    toast(`Community detection failed: ${e.message}`, true);
    $("pref-communities").checked = false;
    view.setCategoryColoring();
    renderCommunityLegend(null);
  }
}

// Loaded set changed while in community mode → recolour (LPA is deterministic, so
// colours stay stable; new nodes get their global community). Debounced.
function scheduleCommunityRecolor() {
  if (view.colorMode() !== "community") return;
  clearTimeout(communityRecolorTimer);
  communityRecolorTimer = setTimeout(() => { applyCommunityColoring().catch(() => {}); }, 300);
}

function resetCommunityColoring() {
  communitySeq++;
  llmCommunityNames.clear();
  const cb = $("pref-communities");
  if (cb) cb.checked = false;
  view.setCategoryColoring();
  renderCommunityLegend(null);
}

// ---- wiring ---------------------------------------------------------------
function wire() {
  refs.picker.addEventListener("change", (e) => selectGraph(e.target.value));

  refs.searchInput.addEventListener("input", onSearchInput);
  refs.searchScope.addEventListener("change", onSearchInput);
  refs.searchMode.addEventListener("click", toggleSearchMode);
  refs.searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { refs.results.innerHTML = ""; refs.searchInput.blur(); }
  });
  // dismiss the results dropdowns when focus leaves their panel
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".command")) refs.results.innerHTML = "";
    if (!e.target.closest(".path-end")) {
      $("path-from-results").innerHTML = "";
      $("path-to-results").innerHTML = "";
    }
  });

  // tabs
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  });

  // display prefs
  $("pref-captions").addEventListener("change", (e) => { prefs.captions = e.target.checked; view.repaint(); });
  $("pref-edge-labels").addEventListener("change", (e) => { prefs.edgeLabels = e.target.checked; view.applyPrefs(); });
  $("pref-communities").addEventListener("change", (e) => setCommunityColoring(e.target.checked));

  // category toolbar
  $("cats-all").addEventListener("click", () => { palette.showAll(); reRenderCats(); refreshEmptyState(); });
  $("cats-none").addEventListener("click", () => {
    palette.hideAll(palette._order.slice()); reRenderCats(); refreshEmptyState();
  });
  $("cats-reset").addEventListener("click", () => { palette.resetColors(); reRenderCats(); });

  // inspector close
  $("insp-close").addEventListener("click", () => { view.select(null); P.closeInspector(refs.inspector); });

  // cypher query
  $("query-run").addEventListener("click", runCypher);
  $("query-clear").addEventListener("click", () => {
    view.clear();
    setVeil(true, "Canvas cleared — run a query, or pick a search result.");
  });
  $("query-input").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runCypher(); }
  });

  // path finder
  ["from", "to"].forEach((kind) => {
    const els = pathEls(kind);
    els.input.addEventListener("input", () => onPathSearch(kind));
    els.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); findPath(); }
      else if (e.key === "Escape") { els.results.innerHTML = ""; }
    });
    els.clear.addEventListener("click", () => { setPathEnd(kind, null); els.input.focus(); });
  });
  $("path-find").addEventListener("click", findPath);
  $("path-clear-btn").addEventListener("click", clearPathHighlight);
  $("path-swap").addEventListener("click", swapPathEnds);

  // multi-selection toolbar
  $("sel-expand").addEventListener("click", expandSelected);
  $("sel-remove").addEventListener("click", removeSelected);
  $("sel-clear").addEventListener("click", () => view.clearSelection());

  // Backspace / Delete removes the current selection from the canvas (same as the
  // toolbar's Remove) — but never while typing in a field, where those keys edit text.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Backspace" && e.key !== "Delete") return;
    if (isTypingTarget(e.target)) return;
    if (view.selectionCount() === 0) return;
    e.preventDefault();
    removeSelected();
  });

  // canvas controls
  $("ctl-expand").addEventListener("click", expandAll);
  $("ctl-clear").addEventListener("click", clearCanvas);
  $("ctl-relayout").addEventListener("click", () => view.runLayout());
  $("ctl-zoom-in").addEventListener("click", () => view.zoomIn());
  $("ctl-zoom-out").addEventListener("click", () => view.zoomOut());
  $("ctl-fit").addEventListener("click", () => view.fit());
}

// Re-render the whole category list (used by the All/None/Reset toolbar, which
// change many rows at once), then reapply the on-canvas counts.
function reRenderCats() {
  P.renderCategories(refs.cats, refs.catsEmpty,
    { nodes: palette._order.map((label) => ({ label })) },
    palette, legendRepaint);
  P.updateCategoryCounts(refs.cats, onCanvasCounts);
  view.repaint();
}

wire();
loadGraphList();
