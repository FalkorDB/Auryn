// DOM controllers for the floating panels: the interactive legend (colour +
// visibility per category), the schema list, the node inspector, search
// results, and the git-sync source cards. These render into elements owned by
// app.js and call back into it for graph actions.

export function esc(v) {
  const d = document.createElement("div");
  d.textContent = v == null ? "" : String(v);
  return d.innerHTML;
}

const isNumeric = (v) => typeof v === "number" || (typeof v === "string" && v !== "" && !isNaN(v) && !isNaN(parseFloat(v)));

// ---------------------------------------------------------------------------
// Legend — categories with a live colour well, a visibility eye, and a count.
// Recolouring and toggling update in place; only structural changes (load,
// reset, all/none) trigger a full re-render, so an open OS colour picker is
// never yanked out from under the user.
// ---------------------------------------------------------------------------
export function renderCategories(listEl, emptyEl, overview, palette, onRepaint) {
  listEl.innerHTML = "";
  const cats = (overview.nodes || []).filter((n) => n.label != null);
  emptyEl.style.display = cats.length ? "none" : "";
  if (!cats.length) return;

  for (const { label } of cats) {
    const on = palette.isVisible(label);
    const li = document.createElement("li");
    li.className = "cat" + (on ? "" : " off");
    li.dataset.label = label;

    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = palette.colorFor(label);
    swatch.title = `Recolour ${label}`;
    const picker = document.createElement("input");
    picker.type = "color";
    picker.value = toHex(palette.colorFor(label));
    picker.setAttribute("aria-label", `Colour for ${label}`);
    picker.addEventListener("input", () => {
      palette.setColor(label, picker.value);
      swatch.style.background = picker.value;
      onRepaint(false); // recolour only — don't reframe the camera
    });
    swatch.appendChild(picker);
    // the swatch is for colour; don't let clicking it also toggle visibility
    swatch.addEventListener("click", (e) => e.stopPropagation());

    const name = document.createElement("span");
    name.className = "cname";
    name.textContent = label;
    name.title = label;

    const cnt = document.createElement("span");
    cnt.className = "ccount";
    cnt.textContent = "0";

    const eye = document.createElement("span"); // state indicator; the row toggles
    eye.className = "eye";
    eye.textContent = on ? "◉" : "○";

    // The whole row toggles this category's visibility — a big, obvious target.
    li.setAttribute("role", "button");
    li.setAttribute("tabindex", "0");
    const apply = () => {
      const nowOn = palette.isVisible(label);
      li.classList.toggle("off", !nowOn);
      eye.textContent = nowOn ? "◉" : "○";
      li.title = nowOn ? `Hide ${label} nodes` : `Show ${label} nodes`;
    };
    li.title = on ? `Hide ${label} nodes` : `Show ${label} nodes`;
    const toggle = () => { palette.toggle(label); apply(); onRepaint(true); };
    li.addEventListener("click", toggle);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });

    li.append(swatch, name, cnt, eye);
    listEl.appendChild(li);
  }
}

// Update per-category counts to reflect what's actually on the canvas, and grey
// out categories that have nothing loaded (so it's obvious their toggle is inert
// until you expand or search some in). Done in place so open colour pickers and
// visibility state aren't disturbed.
export function updateCategoryCounts(listEl, counts) {
  listEl.querySelectorAll(".cat").forEach((li) => {
    const c = counts[li.dataset.label] || 0;
    const cnt = li.querySelector(".ccount");
    if (cnt) cnt.textContent = fmt(c);
    li.classList.toggle("empty", c === 0);
  });
}

export function renderRelationships(listEl, emptyEl, overview) {
  listEl.innerHTML = "";
  const rels = (overview.edges || []).slice().sort((a, b) => b.count - a.count);
  emptyEl.style.display = rels.length ? "none" : "";
  for (const { type, count } of rels) {
    const li = document.createElement("li");
    li.className = "rel";
    li.innerHTML = `<span class="rtype">${esc(type)}</span><span class="rcount">${fmt(count)}</span>`;
    listEl.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Inspector
// ---------------------------------------------------------------------------
export function renderInspector(refs, node, palette, actions, gitCardFor) {
  const color = palette.colorFor(node.label);
  refs.badge.querySelector(".dot").style.background = color;
  refs.badge.querySelector(".cat").textContent = node.label || "—";
  refs.name.textContent = node.name || node.id;

  // actions
  refs.actions.innerHTML = "";
  const mk = (label, title, fn, cls = "") => {
    const b = document.createElement("button");
    b.className = "chip" + (cls ? " " + cls : "");
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", fn);
    return b;
  };
  refs.actions.append(
    mk("Expand", "Load this node's neighbours", () => actions.expand(node.id)),
    mk("Focus", "Center the view here", () => actions.focus(node.id)),
    mk("Hide", "Remove from the canvas", () => actions.hide(node.id), "danger"),
  );
  // Path-finder shortcuts: seed either endpoint of the Path tab from this node.
  if (actions.pathFrom) {
    refs.actions.append(
      mk("Path from", "Use as the path's start", () => actions.pathFrom(node)),
      mk("Path to", "Use as the path's end", () => actions.pathTo(node)),
    );
  }

  // properties
  refs.body.innerHTML = "";
  const props = node.properties || {};
  const keys = Object.keys(props).sort((a, b) => {
    // surface the human-meaningful keys first
    const rank = (k) => (k === "name" ? 0 : k === "id" ? 1 : k === "kind" ? 2 : 5);
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  const group = document.createElement("div");
  group.className = "prop-group";
  if (!keys.length) {
    group.innerHTML = `<p class="placeholder">No attributes.</p>`;
  }
  for (const k of keys) {
    const row = document.createElement("div");
    row.className = "prop";
    const v = props[k];
    row.innerHTML =
      `<span class="k">${esc(k)}</span>` +
      `<span class="v ${isNumeric(v) ? "num" : ""}">${esc(v)}</span>`;
    group.appendChild(row);
  }
  refs.body.appendChild(group);

  // git-sync control for tracked Project nodes
  if (node.label === "Project" && props.repo_uri && gitCardFor) {
    const lbl = document.createElement("p");
    lbl.className = "eyebrow insp-section-label";
    lbl.textContent = "Sync source";
    refs.body.appendChild(lbl);
    refs.body.appendChild(gitCardFor(node));
  }

  refs.root.classList.add("open");
}

export function closeInspector(refs) {
  refs.root.classList.remove("open");
}

// Edges carry attributes too (READS/WRITES columns, CALLS provenance, …), so a
// clicked edge gets the same inspector treatment as a node: its type, its two
// endpoints (each selectable), and its full property table.
export function renderEdgeInspector(refs, edge, from, to, palette, actions) {
  refs.badge.querySelector(".dot").style.background = "#8aa0cf";
  refs.badge.querySelector(".cat").textContent = edge.type || "relationship";
  const fromName = from ? from.name : edge.from;
  const toName = to ? to.name : edge.to;
  refs.name.textContent = `${fromName} → ${toName}`;

  // actions: jump to either endpoint
  refs.actions.innerHTML = "";
  const mk = (label, title, fn) => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", fn);
    return b;
  };
  refs.actions.append(
    mk("Source", `Select ${fromName}`, () => actions.select(edge.from)),
    mk("Target", `Select ${toName}`, () => actions.select(edge.to)),
  );

  refs.body.innerHTML = "";

  // endpoints
  const ends = document.createElement("div");
  ends.className = "prop-group";
  const endRow = (role, node, id) => {
    const color = node ? palette.colorFor(node.label) : "#8b93a1";
    const cat = node ? node.label : "";
    const name = node ? node.name : id;
    return `<div class="prop"><span class="k">${role}</span>` +
      `<span class="v"><span class="end"><span class="edot" style="background:${color}"></span>` +
      `${esc(name)}${cat ? ` <span class="ecat">${esc(cat)}</span>` : ""}</span></span></div>`;
  };
  ends.innerHTML = endRow("from", from, edge.from) + endRow("to", to, edge.to);
  refs.body.appendChild(ends);

  // properties
  const props = edge.properties || {};
  const keys = Object.keys(props).sort();
  const lbl = document.createElement("p");
  lbl.className = "eyebrow insp-section-label";
  lbl.textContent = "Attributes";
  refs.body.appendChild(lbl);

  const group = document.createElement("div");
  group.className = "prop-group";
  if (!keys.length) {
    group.innerHTML = `<p class="placeholder">No attributes on this relationship.</p>`;
  }
  for (const k of keys) {
    const v = props[k];
    const row = document.createElement("div");
    row.className = "prop";
    row.innerHTML = `<span class="k">${esc(k)}</span><span class="v ${isNumeric(v) ? "num" : ""}">${esc(v)}</span>`;
    group.appendChild(row);
  }
  refs.body.appendChild(group);

  refs.root.classList.add("open");
}

// ---------------------------------------------------------------------------
// Search results dropdown
// ---------------------------------------------------------------------------
export function renderSearchResults(listEl, results, palette, onPick) {
  listEl.innerHTML = "";
  if (results == null) return;
  if (!results.length) {
    listEl.innerHTML = `<li class="empty">No matches.</li>`;
    return;
  }
  for (const n of results) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "result";
    btn.setAttribute("role", "option");
    // Semantic results carry a `score` (cosine distance); show it as an intuitive match %.
    const match = n.score != null
      ? `<span class="score" title="semantic match">${Math.round(Math.max(0, 1 - n.score) * 100)}%</span>`
      : "";
    btn.innerHTML =
      `<span class="dot" style="background:${palette.colorFor(n.label)}"></span>` +
      `<span class="name">${esc(n.name)}</span>` +
      `<span class="cat">${esc(n.label)}</span>` + match;
    btn.addEventListener("click", () => onPick(n));
    li.appendChild(btn);
    listEl.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Path finder result — the shortest connection, as a vertical chain of nodes
// joined by their typed hops. Each node is clickable (focus + inspect).
// ---------------------------------------------------------------------------
export function renderPathResult(container, data, palette, actions) {
  container.innerHTML = "";
  if (!data || !data.found) return;
  const nodes = data.nodes || [];
  const edges = data.edges || [];

  const chain = document.createElement("div");
  chain.className = "path-chain";
  nodes.forEach((n, i) => {
    const step = document.createElement("button");
    step.className = "path-node";
    step.title = `Focus ${n.name}`;
    step.innerHTML =
      `<span class="dot" style="background:${palette.colorFor(n.label)}"></span>` +
      `<span class="name mono">${esc(n.name)}</span>` +
      `<span class="cat">${esc(n.label)}</span>`;
    step.addEventListener("click", () => actions.pick(n));
    chain.appendChild(step);

    // the hop from this node to the next, with a glyph showing which way the
    // relationship actually points (an undirected match can traverse it backwards)
    if (i < edges.length) {
      const e = edges[i];
      const forward = e.from === n.id;
      const rel = document.createElement("div");
      rel.className = "path-rel";
      rel.innerHTML =
        `<span class="ln"></span>` +
        `<span class="rtype mono">${forward ? "↓" : "↑"} ${esc(e.type)}</span>`;
      chain.appendChild(rel);
    }
  });
  container.appendChild(chain);
}

// ---------------------------------------------------------------------------
// Git-sync source card (used in the Sources tab and inside the inspector)
// ---------------------------------------------------------------------------
export function buildGitCard(project, ctx) {
  const { api, getGraphName, onSynced, toast } = ctx;
  const props = project.properties;
  const current = props.ref || (props.commit_sha ? props.commit_sha.slice(0, 8) : "?");

  const card = document.createElement("div");
  card.className = "git-card";
  card.innerHTML = `
    <h3>${esc(project.name)}</h3>
    <p class="repo" title="${esc(props.repo_uri || "")}">${esc(props.repo_uri || "")}</p>
    <p class="at">at <code>${esc(current)}</code></p>
    <div class="git-row">
      <select class="ref-select" disabled><option>Loading refs…</option></select>
    </div>
    <input class="ref-custom" type="text" placeholder="commit sha" style="display:none" />
    <div class="git-row">
      <button class="btn-primary sync" disabled>Sync</button>
    </div>
    <p class="status"></p>
  `;

  const select = card.querySelector(".ref-select");
  const custom = card.querySelector(".ref-custom");
  const button = card.querySelector(".sync");
  const status = card.querySelector(".status");
  const setStatus = (msg, kind = "") => { status.textContent = msg; status.className = "status" + (kind ? " " + kind : ""); };

  api.gitRefs(getGraphName(), project.id).then((refs) => {
    const currentRef = props.ref || refs.default_branch;
    const branches = refs.branches
      .map((b) => `<option value="${esc(b)}"${b === currentRef ? " selected" : ""}>${esc(b === refs.default_branch ? b + " (default)" : b)}</option>`)
      .join("");
    const tags = refs.tags.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
    select.innerHTML =
      (branches ? `<optgroup label="Branches">${branches}</optgroup>` : "") +
      (tags ? `<optgroup label="Tags">${tags}</optgroup>` : "") +
      `<option value="__custom__">Other (commit sha)…</option>`;
    select.disabled = false;
    button.disabled = false;
  }).catch((err) => {
    select.innerHTML = `<option value="__custom__" selected>Other (commit sha)…</option>`;
    custom.style.display = "";
    select.disabled = false;
    button.disabled = false;
    setStatus(`Couldn't load refs: ${err.message}`, "err");
  });

  select.addEventListener("change", () => {
    custom.style.display = select.value === "__custom__" ? "" : "none";
  });

  button.addEventListener("click", async () => {
    const ref = select.value === "__custom__" ? custom.value.trim() : select.value;
    button.disabled = true;
    setStatus("Syncing…");
    try {
      const data = await api.gitSync(getGraphName(), project.id, ref || null);
      setStatus(`Synced to ${data.commit_sha.slice(0, 8)} · nodes +${data.nodes_added}/−${data.nodes_removed}/~${data.nodes_updated}`, "ok");
      onSynced();
    } catch (err) {
      setStatus(`Error: ${err.message}`, "err");
      toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  });

  return card;
}

export async function mountGitProjects(container, emptyEl, ctx) {
  const { api, getGraphName } = ctx;
  container.innerHTML = "";
  const name = getGraphName();
  if (!name) { emptyEl.style.display = ""; emptyEl.textContent = "Select a graph to see its tracked sources."; return; }
  let projects = [];
  try { projects = await api.gitProjects(name); } catch { projects = []; }
  if (!projects.length) {
    emptyEl.style.display = "";
    emptyEl.textContent = "No git-tracked sources in this graph.";
    return;
  }
  emptyEl.style.display = "none";
  for (const p of projects) container.appendChild(buildGitCard(p, ctx));
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function fmt(n) {
  if (n == null) return "";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

// native <input type=color> needs a #rrggbb value; pass through if already one
function toHex(c) {
  if (typeof c === "string" && /^#[0-9a-fA-F]{6}$/.test(c)) return c;
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.fillStyle = c || "#8b93a1";
  return ctx.fillStyle.startsWith("#") ? ctx.fillStyle : "#8b93a1";
}
