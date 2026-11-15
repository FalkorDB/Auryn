// GraphView — the WebGL rendering layer.
//
// graphology holds the model; Sigma renders it on the GPU (no per-node DOM, so
// it stays responsive into the tens of thousands of nodes). Everything visual —
// colour, size, dimming, filtering, captions — is decided in the node/edge
// *reducers*, which Sigma runs at render time. That keeps mutation cheap: to
// recolour a category or hide low-degree nodes we just flip a flag and call
// refresh(), never rebuild the graph.
//
// Layout runs as an incremental forceAtlas2 loop driven by requestAnimationFrame
// with a per-frame time budget, so a big graph settles smoothly instead of
// freezing the tab on one synchronous pass.

const Graph = window.graphology;
const FA2 = window.graphologyLibrary.layoutForceAtlas2;
const circularLayout = window.graphologyLibrary.layout.circular;

// Sigma's stock hover renderer paints a light background box; with our light
// caption colour that's light-on-light and unreadable. Draw our own instead: a
// dark pill matching the UI chrome, with near-white text — high contrast on any
// background — plus the node redrawn on top so it stays crisp.
function drawHoverLabel(context, data, settings) {
  const size = settings.labelSize;
  context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;

  if (data.label) {
    const pad = 7;
    const textW = context.measureText(data.label).width;
    const boxH = size + 10;
    const boxX = data.x + data.size + 5;
    const boxY = data.y - boxH / 2;
    const boxW = textW + pad * 2;
    const r = 7;
    context.beginPath();
    context.moveTo(boxX + r, boxY);
    context.arcTo(boxX + boxW, boxY, boxX + boxW, boxY + boxH, r);
    context.arcTo(boxX + boxW, boxY + boxH, boxX, boxY + boxH, r);
    context.arcTo(boxX, boxY + boxH, boxX, boxY, r);
    context.arcTo(boxX, boxY, boxX + boxW, boxY, r);
    context.closePath();
    context.fillStyle = "rgba(16,19,25,0.95)";
    context.fill();
    context.lineWidth = 1;
    context.strokeStyle = "rgba(255,255,255,0.16)";
    context.stroke();

    context.fillStyle = "#eef1f6";
    context.textAlign = "left";
    context.textBaseline = "middle";
    context.fillText(data.label, boxX + pad, data.y + 1);
  }

  // redraw the node above everything, with a subtle ring
  context.beginPath();
  context.arc(data.x, data.y, data.size, 0, Math.PI * 2);
  context.closePath();
  context.fillStyle = data.color || "#8b93a1";
  context.fill();
  context.lineWidth = 2;
  context.strokeStyle = "rgba(255,255,255,0.55)";
  context.stroke();
}

// node display sizing (px, in graph units before camera scaling)
const MIN_SIZE = 4;
const MAX_SIZE = 20;

const DIM_NODE = "rgba(120,128,140,0.16)";
const DIM_LABEL = "#4a515d";
// Sigma blends edges with premultiplied alpha, so any *translucent* edge colour
// piles up toward white where hundreds of spokes converge on a hub — no amount
// of lowering the alpha fixes it. Opaque colours compose "over" and cap at the
// colour itself, so we use solid hex and instead vary the *shade* by degree:
// edges between ordinary nodes are a light slate (clearly visible), a busy hub's
// spokes darken toward the background so its fan recedes instead of glaring.
const EDGE_HL = "#7290c8";
const EDGE_DIM = "#171b23";
// Distinct hues for "colour by community" mode — the dominant communities in
// view get one each (assigned by prevalence), the long tail shares COMMUNITY_OTHER.
const COMMUNITY_PALETTE = [
  "#6ea8fe", "#f2a65a", "#5fd0a6", "#e56b8c", "#b990f5", "#56c9d6", "#d9c46a",
  "#8fbf6a", "#ef8b6a", "#7f9cf5", "#d98ad6", "#6ac2b3", "#c98f7a", "#9aa7ff",
];
const COMMUNITY_OTHER = "#5a6270";
const EDGE_LIGHT = [78, 88, 112];
const EDGE_DARK = [30, 35, 46];
function edgeColor(maxDegree) {
  const t = Math.min(1, Math.max(0, (Math.sqrt(Math.max(1, maxDegree)) - 1) / (Math.sqrt(110) - 1)));
  const mix = (i) => Math.round(EDGE_LIGHT[i] + (EDGE_DARK[i] - EDGE_LIGHT[i]) * t);
  return `rgb(${mix(0)},${mix(1)},${mix(2)})`;
}

export class GraphView {
  constructor(container, opts = {}) {
    this.palette = opts.palette;
    this.prefs = opts.prefs; // { captions, edgeLabels }
    this.onSelect = opts.onSelect || (() => {});       // left-click (highlight)
    this.onInspect = opts.onInspect || (() => {});     // right-click node
    this.onInspectEdge = opts.onInspectEdge || (() => {}); // right-click edge
    this.onExpand = opts.onExpand || (() => {});       // double-click node
    this.onCountsChanged = opts.onCountsChanged || (() => {});
    this.onSelectionChanged = opts.onSelectionChanged || (() => {}); // multi-select

    this.graph = new Graph({ multi: true, type: "directed", allowSelfLoops: true });
    this.nodeData = new Map(); // id -> full serialized node {id,label,name,properties}
    this.edgeData = new Map(); // edgeKey -> {from,to,type,properties}

    this.selectedId = null;
    this.selectedEdgeKey = null;
    this.hoveredId = null;
    this._hoodNodes = null; // Set of ids in hovered/selected neighbourhood, or null
    this._hoodEdges = null;
    this._labelNodes = null; // nodes that should force their caption while focused
    this._pathNodes = null;  // Set of ids on a highlighted shortest path, or null
    this._pathEdges = null;  // Set of edge keys on that path
    this._selNodes = new Set(); // multi-selected node ids (lasso / shift-click / query result)
    this._selEdges = new Set(); // multi-selected edge keys
    this._colorMode = "category";     // "category" (by label) or "community"
    this._nodeCommunity = new Map();  // node id -> community id (community mode)
    this._communityColor = new Map(); // community id -> hex (assigned by prevalence)

    this._animRAF = null;
    // The canvas itself is inset (see app.css #graph) so it *is* the clear area;
    // the camera works in canvas coordinates and needs no extra panel offsets.
    this._insets = { left: 0, right: 0 };

    this.sigma = new window.Sigma(this.graph, container, {
      renderLabels: true,
      labelFont: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
      labelSize: 12,
      labelWeight: "500",
      labelColor: { color: "#c7cedb" },
      labelDensity: 0.7,
      labelGridCellSize: 70,
      labelRenderedSizeThreshold: 6.5,
      edgeLabelFont: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
      edgeLabelSize: 11,
      edgeLabelColor: { color: "#8a93a3" },
      renderEdgeLabels: !!this.prefs.edgeLabels,
      defaultDrawNodeHover: drawHoverLabel,
      enableEdgeEvents: true,
      defaultEdgeType: "arrow",
      defaultNodeColor: "#8b93a1",
      defaultEdgeColor: "#3a4256",
      zIndex: true,
      minCameraRatio: 0.03,
      maxCameraRatio: 18,
      nodeReducer: (node, data) => this._nodeReducer(node, data),
      edgeReducer: (edge, data) => this._edgeReducer(edge, data),
    });

    this._wireEvents();
    this._wireDrag();
  }

  // ---- events -----------------------------------------------------------
  _wireEvents() {
    const s = this.sigma;
    const el = s.getContainer();
    // Suppress the browser context menu so right-click can drive the inspector.
    el.addEventListener("contextmenu", (e) => e.preventDefault());

    s.on("enterNode", ({ node }) => { this._setHovered(node); el.style.cursor = "pointer"; });
    s.on("leaveNode", () => { this._setHovered(null); el.style.cursor = "default"; });

    // Left click just highlights; shift/⌘/ctrl-click toggles into the
    // multi-selection; right click opens the attributes panel; double-click
    // expands. (Bloom-style split of select vs. inspect vs. expand.)
    const additive = (e) => { const o = e && e.event && e.event.original; return !!(o && (o.shiftKey || o.metaKey || o.ctrlKey)); };
    s.on("clickNode", (e) => {
      if (additive(e)) { this.toggleSelect(e.node); return; }
      this.select(e.node); this.onSelect(e.node);
    });
    s.on("clickEdge", (e) => {
      if (additive(e)) { this.toggleSelectEdge(e.edge); return; }
      this.selectEdge(e.edge);
    });
    s.on("rightClickNode", (e) => { e.preventSigmaDefault && e.preventSigmaDefault(); this.select(e.node); this.onInspect(e.node); });
    s.on("rightClickEdge", (e) => { e.preventSigmaDefault && e.preventSigmaDefault(); this.selectEdge(e.edge); this.onInspectEdge(e.edge); });
    s.on("doubleClickNode", (e) => { e.preventSigmaDefault(); this.onExpand(e.node); });
    s.on("clickStage", (e) => {
      // A plain click on empty space clears everything; a modifier-click (the tail
      // of a lasso, or a deliberate add) must NOT wipe the multi-selection.
      if (additive(e)) return;
      const had = this.selectedId || this.selectedEdgeKey;
      this.select(null);
      if (this._selNodes.size || this._selEdges.size) this.clearSelection();
      if (had) this.onSelect(null);
    });
  }

  // Node dragging. Sigma has no built-in drag, so we drive it off the mouse
  // captor: grab on downNode, follow the cursor in graph coordinates on move,
  // release on mouseup. We freeze the normalisation bbox for the duration so the
  // whole graph doesn't rescale as the node is pulled past the current extent
  // (which would make it slip out from under the cursor).
  _wireDrag() {
    const s = this.sigma;
    const cap = s.getMouseCaptor();
    const el = s.getContainer();
    this._dragging = false;
    let dragged = null;

    // --- lasso box-select state ---
    let boxing = false, bx0 = 0, by0 = 0, boxEl = null, boxAdditive = false;
    const containerXY = (orig) => {
      const r = el.getBoundingClientRect();
      return { x: orig.clientX - r.left, y: orig.clientY - r.top };
    };
    const drawBox = (x, y) => {
      boxEl.style.left = Math.min(bx0, x) + "px";
      boxEl.style.top = Math.min(by0, y) + "px";
      boxEl.style.width = Math.abs(x - bx0) + "px";
      boxEl.style.height = Math.abs(y - by0) + "px";
    };

    s.on("downNode", (e) => {
      // Left button only, and NOT with a modifier (shift/⌘/ctrl-click is a
      // multi-select toggle, not a drag). Right/middle buttons never grab —
      // otherwise the node would stick to the cursor (captor mouseup doesn't fire).
      const o = e.event && e.event.original;
      const button = o ? o.button : 0;
      if (button !== 0 || (o && (o.shiftKey || o.metaKey || o.ctrlKey))) return;
      dragged = e.node;
      this._dragging = true;
      s.setCustomBBox(s.getBBox());
      el.style.cursor = "grabbing";
    });

    // Ctrl-drag on empty canvas draws a selection lasso (⌘ is also accepted
    // because on macOS Ctrl+click is the OS right-click). On a node it's a drag,
    // and hoveredId being set is how we tell "started on a node" apart. Adding
    // Shift makes the lasso EXTEND the current selection (file-manager style);
    // without it a fresh lasso REPLACES the selection.
    cap.on("mousedown", (e) => {
      const o = e.original;
      if (!o || !(o.ctrlKey || o.metaKey) || this.hoveredId || dragged) return;
      boxing = true;
      boxAdditive = !!o.shiftKey;
      const p = containerXY(o);
      bx0 = p.x; by0 = p.y;
      boxEl = document.createElement("div");
      boxEl.className = "select-box";
      el.appendChild(boxEl);
      drawBox(bx0, by0);
      if (e.preventSigmaDefault) e.preventSigmaDefault();
      o.preventDefault();
    });

    cap.on("mousemovebody", (e) => {
      if (boxing) {
        const p = containerXY(e.original);
        drawBox(p.x, p.y);
        e.preventSigmaDefault();
        e.original.preventDefault();
        e.original.stopPropagation();
        return;
      }
      if (!dragged) return;
      const pos = s.viewportToGraph(e);
      this.graph.setNodeAttribute(dragged, "x", pos.x);
      this.graph.setNodeAttribute(dragged, "y", pos.y);
      // stop the camera from panning while we're dragging a node
      e.preventSigmaDefault();
      e.original.preventDefault();
      e.original.stopPropagation();
    });

    const endDrag = () => {
      if (!dragged) return;
      dragged = null;
      this._dragging = false;
      s.setCustomBBox(null);
      el.style.cursor = "default";
    };
    const endBox = () => {
      if (!boxing) return;
      boxing = false;
      const cr = el.getBoundingClientRect();
      const r = boxEl.getBoundingClientRect();
      const box = { x0: r.left - cr.left, y0: r.top - cr.top, x1: r.right - cr.left, y1: r.bottom - cr.top };
      el.removeChild(boxEl);
      boxEl = null;
      // ignore an accidental micro-drag (that's really a click)
      if (box.x1 - box.x0 > 3 || box.y1 - box.y0 > 3) this._applyBoxSelection(box, boxAdditive);
    };
    const end = () => { endBox(); endDrag(); };
    cap.on("mouseup", end);
    cap.on("mouseleave", end);
    // backstop: any mouseup anywhere releases, so a node can never stay stuck to
    // the cursor even if Sigma's captor misses the event (e.g. right button).
    window.addEventListener("mouseup", end);
  }

  // Nodes whose screen position falls inside the lasso rectangle become the
  // selection, plus any edge with both endpoints inside. Only currently-visible
  // nodes are eligible (you can't lasso something you can't see). `additive`
  // (Shift held) EXTENDS the current selection; otherwise the lasso REPLACES it
  // — the file-manager convention, so consecutive lassos don't silently union.
  _applyBoxSelection(box, additive = false) {
    const inNodes = [];
    this.graph.forEachNode((id, d) => {
      if (!this._passesFilter(id, d)) return;
      const p = this.sigma.graphToViewport({ x: d.x, y: d.y });
      if (p.x >= box.x0 && p.x <= box.x1 && p.y >= box.y0 && p.y <= box.y1) inNodes.push(id);
    });
    const inSet = new Set(inNodes);
    const inEdges = [];
    this.graph.forEachEdge((edge, _attr, src, tgt) => {
      if (inSet.has(src) && inSet.has(tgt)) inEdges.push(edge);
    });
    if (additive) this.addToSelection(inNodes, inEdges);
    else this.setSelection(inNodes, inEdges); // replace (no fit — don't jump the camera)
  }

  // ---- reducers ---------------------------------------------------------
  _passesFilter(node, data) {
    return this.palette.isVisible(data.category);
  }

  _nodeReducer(node, data) {
    // A node on the highlighted path or in the multi-selection is always shown,
    // even if its category is hidden — the user asked to see those specifically.
    const onPath = this._pathNodes ? this._pathNodes.has(node) : false;
    const onSel = this._selNodes.size ? this._selNodes.has(node) : false;
    // Always carry x/y, even when hidden: Sigma keeps the reduced data in its
    // node cache, and if a hidden node (no position) is later shown, its refresh
    // throws "could not find a valid position". Including x/y makes hide→show safe.
    if (!onPath && !onSel && !this._passesFilter(node, data)) return { hidden: true, x: data.x, y: data.y };

    const res = {
      x: data.x, y: data.y, size: data.size,
      label: this.prefs.captions ? data.label : null,
      color: this._baseColor(node, data),
      zIndex: 1,
    };

    // Path nodes keep their CATEGORY colour, consistent with the selection /
    // query-result highlight — they're emphasised by a bigger size and a forced
    // caption, not by recolouring; everything off the path greys out below.
    if (onPath) {
      res.label = data.label;
      res.forceLabel = true;
      res.zIndex = 4;
      const focused = node === (this.hoveredId || this.selectedId);
      res.size = focused ? data.size * 1.5 + 2 : data.size * 1.2 + 1.5;
      return res;
    }

    const hood = this._hoodNodes;
    const inHood = hood ? hood.has(node) : false;
    // Focus mode = something is drawing attention to a subset (a selection, a
    // path, or a hover neighbourhood). Outside focus mode the graph is full colour.
    const focusMode = this._selNodes.size > 0 || !!this._pathNodes || !!hood;
    if (!focusMode) return res;

    // In focus: the selected set and the hovered neighbourhood keep their category
    // colours (just emphasised); everything else recedes to grey.
    if (onSel) {
      res.zIndex = 3;
      res.size = data.size * 1.25 + 1;
      // Force captions only for a small selection — a big lasso of forced labels
      // is just noise (same rule the hover neighbourhood uses).
      if (this._selNodes.size <= 25) { res.label = data.label; res.forceLabel = true; }
      return res;
    }
    if (inHood) {
      res.zIndex = 2;
      // Only _labelNodes (the focused node / an edge's endpoints) force a caption.
      if (this._labelNodes && this._labelNodes.has(node)) {
        res.label = data.label;
        res.forceLabel = true;
      } else {
        res.label = null;
      }
      if (node === (this.hoveredId || this.selectedId)) {
        res.size = data.size * 1.5 + 2;
        res.zIndex = 3;
      }
      return res;
    }
    // Not in any focused set -> recede (grey out).
    res.color = DIM_NODE;
    res.label = null;
    res.zIndex = 0;
    return res;
  }

  _edgeReducer(edge, data) {
    const ext = this.graph.extremities(edge);
    const a = this.nodeData.get(ext[0]);
    const b = this.nodeData.get(ext[1]);
    const onPath = this._pathEdges ? this._pathEdges.has(edge) : false;
    const onSel = this._selEdges.size ? this._selEdges.has(edge) : false;
    // an edge whose two endpoints are both selected counts as part of the
    // selected subgraph even if it wasn't explicitly picked
    const bothSel = this._selNodes.size > 0 && this._selNodes.has(ext[0]) && this._selNodes.has(ext[1]);
    // hide edge if either endpoint is filtered out — unless the edge is
    // highlighted (path / selected / between two selected nodes), then always draw.
    if (!onPath && !onSel && !bothSel) {
      if (a && !this._passesFilter(ext[0], { category: a.label })) return { hidden: true };
      if (b && !this._passesFilter(ext[1], { category: b.label })) return { hidden: true };
    }

    // Path edges use the standard highlight colour (same as selection/hover
    // edges) — just a touch thicker, and with their relationship-type label
    // forced on so each hop reads. Everything out of focus greys out.
    if (onPath) return { color: EDGE_HL, size: 3.2, zIndex: 4, label: data.label };

    const showLabel = this.prefs.edgeLabels ? data.label : null;
    const hoodE = this._hoodEdges;
    const inHoodE = hoodE ? hoodE.has(edge) : false;
    const focusMode =
      this._selNodes.size > 0 || this._selEdges.size > 0 || !!this._pathEdges || !!hoodE;
    if (!focusMode) {
      const md = Math.max(this.graph.degree(ext[0]), this.graph.degree(ext[1]));
      // width ~2 so edges are actually right-clickable; opaque colour keeps a
      // dense hub from glaring even at this thickness.
      return { color: edgeColor(md), size: 2, label: showLabel };
    }
    if (onSel || bothSel || inHoodE) {
      return { color: EDGE_HL, size: 2.4, zIndex: 2, label: showLabel };
    }
    return { color: EDGE_DIM, size: 1, label: null };
  }

  // ---- hover / selection ------------------------------------------------
  _setHovered(id) {
    this.hoveredId = id;
    this._recomputeHood();
    this.sigma.refresh();
  }

  select(id) {
    this.selectedId = id || null;
    this.selectedEdgeKey = null;
    this._recomputeHood();
    this.sigma.refresh();
  }

  selectEdge(key) {
    this.selectedEdgeKey = this.graph.hasEdge(key) ? key : null;
    this.selectedId = null;
    this._recomputeHood();
    this.sigma.refresh();
  }

  getEdge(key) { return this.edgeData.get(key); }

  // ---- path highlight ---------------------------------------------------
  // Draw a shortest path (from the /path API) onto the canvas: merge its nodes
  // and edges in, then light the whole chain green with the rest of the graph
  // dimmed, and frame it. The path stays lit until cleared or until new nodes
  // arrive by other means (see merge()).
  showPath(nodes = [], edges = []) {
    this._clearPathState();
    // merge under a guard so merge() doesn't treat these newcomers as a reason
    // to clear the (not-yet-set) path highlight.
    this._showingPath = true;
    try {
      this.merge(nodes, edges, { animate: true });
    } finally {
      this._showingPath = false;
    }
    this._pathNodes = new Set(nodes.map((n) => n.id));
    this._pathEdges = new Set(edges.map((e) => `${e.from}|${e.type}|${e.to}`));
    // Force relationship labels on so each hop's type reads (e.g. CALLS,
    // REFERENCES); off-path edges return label:null so only the path is labelled.
    this.sigma.setSetting("renderEdgeLabels", true);
    this.selectedId = null; this.selectedEdgeKey = null; this.hoveredId = null;
    this._recomputeHood();
    this.sigma.refresh();
    const ids = this._pathNodes;
    this._fitAfterLayout(ids, 650, () => this._pathNodes === ids);
  }

  // Frame `ids` once the incremental layout has settled — fitting mid-animation
  // would frame nodes still springing out from the origin (so the camera lands
  // on the wrong spot). Falls through quickly when no layout is running.
  _fitAfterLayout(ids, duration, stillValid) {
    const step = () => {
      if (!stillValid()) return;
      if (this._animRAF) { setTimeout(step, 80); return; }
      this.fit(duration, ids);
    };
    setTimeout(step, 120);
  }

  hasPath() { return !!this._pathNodes; }

  // Reset the highlight state and restore the edge-label setting to the user's
  // pref, without touching the graph data — the path nodes stay on the canvas.
  _clearPathState() {
    if (!this._pathNodes && !this._pathEdges) return;
    this._pathNodes = null;
    this._pathEdges = null;
    this.sigma.setSetting("renderEdgeLabels", !!this.prefs.edgeLabels);
  }

  clearPath() {
    this._clearPathState();
    this.sigma.refresh();
  }

  // ---- multi-selection --------------------------------------------------
  // A set of nodes/edges highlighted amber, driven by shift-click, the lasso, or
  // a query result. Independent of the single left-click highlight and the path.
  toggleSelect(id) {
    if (!this.graph.hasNode(id)) return;
    this._selNodes.has(id) ? this._selNodes.delete(id) : this._selNodes.add(id);
    this._afterSelectionChange();
  }
  toggleSelectEdge(key) {
    if (!this.graph.hasEdge(key)) return;
    this._selEdges.has(key) ? this._selEdges.delete(key) : this._selEdges.add(key);
    this._afterSelectionChange();
  }
  addToSelection(nodeIds = [], edgeKeys = []) {
    for (const id of nodeIds) if (this.graph.hasNode(id)) this._selNodes.add(id);
    for (const k of edgeKeys) if (this.graph.hasEdge(k)) this._selEdges.add(k);
    this._afterSelectionChange();
  }
  // Replace the whole selection (used for query results). `fit` frames it.
  setSelection(nodeIds = [], edgeKeys = [], { fit = false } = {}) {
    this._selNodes = new Set(nodeIds.filter((id) => this.graph.hasNode(id)));
    this._selEdges = new Set(edgeKeys.filter((k) => this.graph.hasEdge(k)));
    this._afterSelectionChange();
    if (fit && this._selNodes.size) {
      const ids = this._selNodes;
      this._fitAfterLayout(ids, 650, () => this._selNodes === ids);
    }
  }
  clearSelection() {
    if (!this._selNodes.size && !this._selEdges.size) return;
    this._selNodes = new Set();
    this._selEdges = new Set();
    this._afterSelectionChange();
  }
  getSelection() { return { nodes: [...this._selNodes], edges: [...this._selEdges] }; }
  selectionCount() { return this._selNodes.size + this._selEdges.size; }

  // Drop every selected node (and its incident edges) and every selected edge
  // from the canvas — client-side only, the graph in the DB is untouched.
  removeSelection() {
    const nodes = [...this._selNodes];
    const edges = [...this._selEdges];
    for (const k of edges) if (this.graph.hasEdge(k)) { this.graph.dropEdge(k); this.edgeData.delete(k); }
    for (const id of nodes) if (this.graph.hasNode(id)) { this.graph.dropNode(id); this.nodeData.delete(id); }
    this._selNodes = new Set();
    this._selEdges = new Set();
    if (this.selectedId && !this.graph.hasNode(this.selectedId)) this.selectedId = null;
    this._resize();
    this._recomputeHood();
    this.onSelectionChanged(0, 0);
    this._emitCounts();
    this.sigma.refresh();
    return { nodes: nodes.length, edges: edges.length };
  }

  _afterSelectionChange() {
    this._recomputeHood();
    this.onSelectionChanged(this._selNodes.size, this._selEdges.size);
    this.sigma.refresh();
  }

  // ---- colour mode (category vs community) ------------------------------
  // The node's base colour: by category label (default), or by detected
  // community. Focus overlays (selection/path/hood) sit on top of whichever
  // this returns, so "keep the entity's colour" means the active mode's colour.
  _baseColor(node, data) {
    if (this._colorMode === "community") {
      const cid = this._nodeCommunity.get(node);
      return (cid != null && this._communityColor.get(cid)) || COMMUNITY_OTHER;
    }
    return this.palette.colorFor(data.category);
  }

  // Switch to community colouring. `idToCommunity` maps node id -> community id
  // (from the /communities API). The dominant communities *in the current view*
  // get a distinct palette colour each (ranked by how many on-canvas nodes they
  // hold); the long tail shares COMMUNITY_OTHER. Returns the ranked list
  // [[communityId, count], ...] so the app can build a legend.
  setCommunities(idToCommunity) {
    this._nodeCommunity = idToCommunity instanceof Map
      ? idToCommunity : new Map(Object.entries(idToCommunity));
    const counts = new Map();
    this.graph.forEachNode((id) => {
      const c = this._nodeCommunity.get(id);
      if (c != null) counts.set(c, (counts.get(c) || 0) + 1);
    });
    const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    this._communityColor = new Map();
    ranked.forEach(([cid], i) => {
      this._communityColor.set(cid, i < COMMUNITY_PALETTE.length ? COMMUNITY_PALETTE[i] : COMMUNITY_OTHER);
    });
    this._colorMode = "community";
    this.sigma.refresh();
    return ranked;
  }
  setCategoryColoring() { this._colorMode = "category"; this.sigma.refresh(); }
  colorMode() { return this._colorMode; }
  communityOf(id) { return this._nodeCommunity.get(id); }
  communityColorFor(cid) { return this._communityColor.get(cid) || COMMUNITY_OTHER; }

  // The "focused" node is whatever is hovered, else whatever is selected.
  // Its neighbourhood (itself + adjacent nodes + incident edges) stays lit; the
  // rest dims. null neighbourhood => nothing focused => everything at full.
  _recomputeHood() {
    const focus = this.hoveredId || this.selectedId;
    if (focus && this.graph.hasNode(focus)) {
      const nodes = new Set([focus]);
      const edges = new Set();
      this.graph.forEachEdge(focus, (edge, _a, src, tgt) => {
        edges.add(edge);
        nodes.add(src); nodes.add(tgt);
      });
      this._hoodNodes = nodes;
      this._hoodEdges = edges;
      this._labelNodes = new Set([focus]);
      return;
    }
    // an edge is selected: light its two endpoints (both labelled) and itself
    if (this.selectedEdgeKey && this.graph.hasEdge(this.selectedEdgeKey)) {
      const [s, t] = this.graph.extremities(this.selectedEdgeKey);
      this._hoodNodes = new Set([s, t]);
      this._hoodEdges = new Set([this.selectedEdgeKey]);
      this._labelNodes = new Set([s, t]);
      return;
    }
    this._hoodNodes = null;
    this._hoodEdges = null;
    this._labelNodes = null;
  }

  // ---- data mutation ----------------------------------------------------
  _degreeSize(id) {
    const d = this.graph.degree(id);
    // sqrt keeps a hub from dwarfing everything while still reading as bigger
    return Math.min(MAX_SIZE, MIN_SIZE + Math.sqrt(d) * 2.1);
  }

  _resize() {
    this.graph.forEachNode((id) => {
      this.graph.setNodeAttribute(id, "size", this._degreeSize(id));
    });
  }

  // Add nodes/edges. New nodes get an initial position near `spawn` (an existing
  // node id to grow from) or scattered near the origin for a fresh load.
  merge(nodes = [], edges = [], { spawn = null, layout = true, animate = false } = {}) {
    let added = 0;
    const hasSpawn = spawn && this.graph.hasNode(spawn);
    const sx = hasSpawn ? this.graph.getNodeAttribute(spawn, "x") : 0;
    const sy = hasSpawn ? this.graph.getNodeAttribute(spawn, "y") : 0;

    // Spread new neighbours evenly on a RING around the spawn node rather than
    // dumping them all within a tiny random radius (which piled a high-degree
    // node's neighbours on top of each other and left forceAtlas2 to untangle an
    // overlap it never fully resolves). The ring radius grows with the fan-out so
    // 5 neighbours and 80 neighbours both land un-crowded, and a random angular
    // offset keeps repeated expansions from stacking along the same spoke.
    const fanOut = hasSpawn ? nodes.reduce((k, n) => k + (this.graph.hasNode(n.id) ? 0 : 1), 0) : 0;
    const ringR = 24 + 9 * fanOut / (2 * Math.PI);   // circumference ∝ count → even spacing
    const angStep = fanOut ? (2 * Math.PI) / fanOut : 0;
    const angOff = Math.random() * Math.PI * 2;
    let spoke = 0;

    for (const n of nodes) {
      this.nodeData.set(n.id, n);
      if (!this.graph.hasNode(n.id)) {
        let x, y;
        if (hasSpawn) {
          const ang = angOff + spoke * angStep + (Math.random() - 0.5) * angStep * 0.35;
          const r = ringR * (0.82 + Math.random() * 0.36);  // slight radial jitter, not a rigid circle
          x = sx + Math.cos(ang) * r;
          y = sy + Math.sin(ang) * r;
          spoke++;
        } else {
          const ang = Math.random() * Math.PI * 2;
          const r = Math.random() * 120;
          x = Math.cos(ang) * r;
          y = Math.sin(ang) * r;
        }
        this.graph.addNode(n.id, { x, y, size: MIN_SIZE, label: n.name, category: n.label });
        added++;
      } else {
        this.graph.mergeNodeAttributes(n.id, { label: n.name, category: n.label });
      }
    }

    for (const e of edges) {
      if (!this.graph.hasNode(e.from) || !this.graph.hasNode(e.to)) continue;
      const key = `${e.from}|${e.type}|${e.to}`;
      this.edgeData.set(key, e);
      if (!this.graph.hasEdgeWithKey || !this.graph.hasEdge(key)) {
        try {
          this.graph.mergeEdgeWithKey(key, e.from, e.to, { label: e.type, type: "arrow" });
        } catch { /* self-loop or dup — ignore */ }
      }
    }

    // Bringing in new nodes by any means other than showPath() invalidates a
    // shown path highlight (the newcomers aren't on it), so the canvas would read
    // as "everything new is dimmed/off-path" — drop the highlight in that case.
    if (added > 0 && this._pathNodes && !this._showingPath) this._clearPathState();

    this._resize();
    this._emitCounts();
    if (added > 0 && layout) {
      if (animate) this.animateLayout();
      else this.runLayout();
    } else {
      this.sigma.refresh();
    }
    return added;
  }

  nodeIds() { return [...this.nodeData.keys()]; }

  setData(nodes, edges) {
    this.stopLayout();
    this.graph.clear();
    this.nodeData.clear();
    this.edgeData.clear();
    this.selectedId = null; this.hoveredId = null;
    this._hoodNodes = null; this._hoodEdges = null;
    this._clearPathState();
    this._selNodes = new Set(); this._selEdges = new Set();
    this._colorMode = "category"; this._nodeCommunity = new Map(); this._communityColor = new Map();
    this.onSelectionChanged(0, 0);
    this.sigma.getCamera().setState(this._FRAME);
    this.merge(nodes, edges);
  }

  clear() {
    this.stopLayout();
    this.graph.clear();
    this.nodeData.clear();
    this.edgeData.clear();
    this.selectedId = null; this.hoveredId = null;
    this._hoodNodes = null; this._hoodEdges = null;
    this._clearPathState();
    this._selNodes = new Set(); this._selEdges = new Set();
    this._colorMode = "category"; this._nodeCommunity = new Map(); this._communityColor = new Map();
    this.onSelectionChanged(0, 0);
    this._emitCounts();
    this.sigma.refresh();
  }

  hideNode(id) {
    if (!this.graph.hasNode(id)) return;
    this.graph.dropNode(id);
    this.nodeData.delete(id);
    if (this.selectedId === id) this.selectedId = null;
    if (this._selNodes.delete(id)) this.onSelectionChanged(this._selNodes.size, this._selEdges.size);
    this._resize();
    this._recomputeHood();
    this._emitCounts();
    this.sigma.refresh();
  }

  _emitCounts() {
    // per-category tally of what's actually on the canvas, so the legend can
    // show real counts and grey out categories that aren't loaded yet.
    const counts = {};
    for (const n of this.nodeData.values()) counts[n.label] = (counts[n.label] || 0) + 1;
    this.onCountsChanged(this.graph.order, this.graph.size, counts);
  }

  order() { return this.graph.order; }
  hasNode(id) { return this.graph.hasNode(id); }
  getNode(id) { return this.nodeData.get(id); }

  // How many loaded nodes currently survive the category/min-degree filters —
  // lets the app tell "nothing loaded" apart from "everything filtered out".
  visibleCount() {
    let n = 0;
    this.graph.forEachNode((id, data) => { if (this._passesFilter(id, data)) n++; });
    return n;
  }

  // ---- layout (incremental, non-blocking) -------------------------------
  runLayout() {
    if (this.graph.order === 0) return;
    const s = FA2.inferSettings(this.graph);
    s.adjustSizes = true;
    s.barnesHutOptimize = this.graph.order > 800;
    s.gravity = 0.45;      // weaker inward pull so expanded fans keep their spread
    s.scalingRatio = 18;   // stronger node-node repulsion -> less crowding/overlap
    s.slowDown = 1 + Math.log(this.graph.order + 1);
    // Synchronous: compute the whole layout, THEN render once. Sigma re-fits the
    // graph to the canvas on every refresh, so animating the layout frame-by-
    // frame makes the whole graph visibly drift/rescale as it expands. Computing
    // it fully and refreshing a single time means the graph simply appears in its
    // settled position — no camera motion, no whole-graph shift on load.
    const iters = this.graph.order > 2000 ? 100 : this.graph.order > 500 ? 200 : 320;
    FA2.assign(this.graph, { iterations: iters, settings: s });
    this.sigma.refresh();
  }

  // Animated variant, used for interactive expansion (double-click, search
  // pick). Here the graph is already settled and we're adding a handful of nodes
  // near an existing one, so the bounding box barely changes — Sigma's re-fit is
  // negligible and what you see is the new nodes springing out and settling.
  // (On first load the box explodes from a tiny cluster to a wide spread, which
  // is why load uses the synchronous runLayout() instead — see the note there.)
  animateLayout() {
    if (this.graph.order === 0) return;
    const s = FA2.inferSettings(this.graph);
    s.adjustSizes = true;
    s.barnesHutOptimize = this.graph.order > 800;
    s.gravity = 0.45;      // weaker inward pull so expanded fans keep their spread
    s.scalingRatio = 18;   // stronger node-node repulsion -> less crowding/overlap
    // Snappy, not slow-motion: a low fixed slowDown means each iteration actually
    // moves nodes a visible amount (the old `1 + log(order)` damped them to a crawl),
    // over a shorter frame budget so a small expand settles in ~half a second.
    s.slowDown = 2.2;
    this._stopAnim();
    const perFrame = this.graph.order > 3000 ? 2 : this.graph.order > 800 ? 4 : 6;
    let framesLeft = 42;
    const step = () => {
      const t0 = performance.now();
      let it = 0;
      do {
        FA2.assign(this.graph, { iterations: perFrame, settings: s });
        it += perFrame;
      } while (performance.now() - t0 < 10 && it < 40);
      this.sigma.refresh();
      if (--framesLeft <= 0) { this._animRAF = null; return; }
      this._animRAF = requestAnimationFrame(step);
    };
    this._animRAF = requestAnimationFrame(step);
  }

  _stopAnim() {
    if (this._animRAF) { cancelAnimationFrame(this._animRAF); this._animRAF = null; }
  }

  stopLayout() { this._stopAnim(); }

  // ---- camera -----------------------------------------------------------
  // animatedReset() corrupts camera state to null in this Sigma build, so we
  // compute an explicit state instead.
  _FRAME = { x: 0.5, y: 0.5, ratio: 1, angle: 0 };

  // Frame the currently *visible* nodes (not the whole coordinate space, which
  // includes filtered-out nodes) into the clear area of the canvas — i.e. the
  // region not covered by the floating left panel / right inspector — so content
  // never ends up tiny in a corner or hidden behind a panel. The camera solve is
  // done by probing how viewport position responds to camera x/y (Sigma's
  // transform is affine), which avoids depending on its internal scale constants.
  fit(duration = 500, onlyIds = null, minRatio = null) {
    const cam = this.sigma.getCamera();
    const ids = [];
    let gx0 = Infinity, gy0 = Infinity, gx1 = -Infinity, gy1 = -Infinity;
    this.graph.forEachNode((id, d) => {
      // When framing an explicit set (a path or a selection) show all of it,
      // even categories that are hidden; otherwise frame only visible nodes.
      if (onlyIds) { if (!onlyIds.has(id)) return; }
      else if (!this._passesFilter(id, d)) return;
      ids.push(id);
      if (d.x < gx0) gx0 = d.x; if (d.x > gx1) gx1 = d.x;
      if (d.y < gy0) gy0 = d.y; if (d.y > gy1) gy1 = d.y;
    });
    if (!ids.length) { cam.setState(this._FRAME); return; }

    const dims = this.sigma.getDimensions();
    const narrow = dims.width < 820;
    const pad = 64;
    const insetL = narrow ? 0 : this._insets.left;
    const insetR = narrow ? 0 : this._insets.right;
    const tx0 = insetL + pad, tx1 = dims.width - insetR - pad;
    const ty0 = pad, ty1 = dims.height - pad;
    const tcx = (tx0 + tx1) / 2, tcy = (ty0 + ty1) / 2;
    const tw = Math.max(60, tx1 - tx0), th = Math.max(60, ty1 - ty0);

    // measure the visible bbox in viewport px at a reference camera state.
    // NB: graphToViewport() reads a transform matrix that Sigma only recomputes
    // on refresh(), NOT on camera.setState() — so every setState used purely for
    // measurement must be followed by refresh() or graphToViewport returns stale
    // coordinates (base==probe below → the centre solve degenerates to the graph
    // centre, which silently "works" for fit-all but sends an off-centre subset
    // off-screen). These refreshes are synchronous within one tick, so nothing
    // paints until the cam.animate() at the end.
    cam.setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
    this.sigma.refresh();
    let vx0 = Infinity, vy0 = Infinity, vx1 = -Infinity, vy1 = -Infinity;
    for (const id of ids) {
      const a = this.graph.getNodeAttributes(id);
      const p = this.sigma.graphToViewport({ x: a.x, y: a.y });
      if (p.x < vx0) vx0 = p.x; if (p.x > vx1) vx1 = p.x;
      if (p.y < vy0) vy0 = p.y; if (p.y > vy1) vy1 = p.y;
    }
    const vw = Math.max(1, vx1 - vx0), vh = Math.max(1, vy1 - vy0);
    let ratio = Math.max(vw / tw, vh / th);
    // Clamp the zoom. The lower bound (max zoom-IN) matters most when framing a
    // small SUBSET (a selection or path): if that subset is tiny relative to the
    // whole graph's extent — e.g. a short path disconnected from everything else,
    // which collapses to a near-point — an unbounded fit zooms in so far that the
    // tiniest camera-solve error puts it off-screen ("fit landed on empty space").
    // A higher floor keeps surrounding context, so the target is always visible.
    const floor = minRatio != null ? minRatio : (onlyIds ? 0.35 : 0.08);
    ratio = Math.min(20, Math.max(floor, ratio));

    // solve camera x/y so the visible bbox centre lands on the target centre
    const gcx = (gx0 + gx1) / 2, gcy = (gy0 + gy1) / 2;
    cam.setState({ x: 0.5, y: 0.5, ratio, angle: 0 });
    this.sigma.refresh();
    const base = this.sigma.graphToViewport({ x: gcx, y: gcy });
    cam.setState({ x: 0.6, y: 0.6, ratio, angle: 0 });
    this.sigma.refresh();
    const probe = this.sigma.graphToViewport({ x: gcx, y: gcy });
    const dPx = (probe.x - base.x) / 0.1;
    const dPy = (probe.y - base.y) / 0.1;
    const target = {
      x: dPx ? 0.5 + (tcx - base.x) / dPx : 0.5,
      y: dPy ? 0.5 + (tcy - base.y) / dPy : 0.5,
      ratio, angle: 0,
    };
    if (duration <= 0) cam.setState(target);
    else cam.animate(target, { duration });
  }
  zoomIn() {
    const cam = this.sigma.getCamera();
    cam.animate({ ratio: Math.max(0.03, cam.getState().ratio / 1.4) }, { duration: 200 });
  }
  zoomOut() {
    const cam = this.sigma.getCamera();
    cam.animate({ ratio: Math.min(18, cam.getState().ratio * 1.4) }, { duration: 200 });
  }

  focus(id) {
    if (!this.graph.hasNode(id)) return;
    const d = this.sigma.getNodeDisplayData(id);
    if (!d) return;
    this.sigma.getCamera().animate({ x: d.x, y: d.y, ratio: 0.35 }, { duration: 500 });
  }

  // ---- pref/palette-driven repaint --------------------------------------
  applyPrefs() {
    this.sigma.setSetting("renderEdgeLabels", !!this.prefs.edgeLabels);
    this.sigma.refresh();
  }
  repaint() { this.sigma.refresh(); }

  destroy() { this.stopLayout(); this.sigma.kill(); }
}
