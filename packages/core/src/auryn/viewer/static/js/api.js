// Thin client over the viewer's read-only JSON API (plus the one git-sync write).
// Every method returns parsed JSON or throws an Error carrying the server message.

async function req(url, options) {
  const res = await fetch(url, options);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    const msg = (data && data.error) || `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return data;
}

const g = (name) => `/api/graphs/${encodeURIComponent(name)}`;

export const Api = {
  listGraphs: () => req("/api/graphs"),
  overview: (name) => req(`${g(name)}/overview`),
  roots: (name) => req(`${g(name)}/roots`),
  node: (name, id) => req(`${g(name)}/node?id=${encodeURIComponent(id)}`),
  neighbors: (name, id) => req(`${g(name)}/neighbors?id=${encodeURIComponent(id)}`),
  all: (name, limit) => req(`${g(name)}/all${limit ? `?limit=${limit}` : ""}`),
  path: (name, { from, to, maxDepth, directed = true } = {}) => {
    const p = new URLSearchParams();
    p.set("from", from);
    p.set("to", to);
    if (maxDepth) p.set("max_depth", String(maxDepth));
    p.set("directed", directed ? "true" : "false");
    return req(`${g(name)}/path?${p.toString()}`);
  },
  search: (name, { label, q, limit } = {}) => {
    const p = new URLSearchParams();
    if (label) p.set("label", label);
    if (q) p.set("q", q);
    if (limit) p.set("limit", String(limit));
    return req(`${g(name)}/search?${p.toString()}`);
  },
  searchSemantic: (name, { q, k } = {}) => {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (k) p.set("k", String(k));
    return req(`${g(name)}/search-semantic?${p.toString()}`);
  },
  runQuery: (name, cypher) =>
    req(`${g(name)}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cypher }),
    }),
  communities: (name, ids) =>
    req(`${g(name)}/communities`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    }),
  communityNames: (name, communities) =>
    req(`${g(name)}/community-names`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ communities }),
    }),
  gitProjects: (name) => req(`${g(name)}/git-projects`),
  gitRefs: (name, projectId) => req(`${g(name)}/git-refs?project_id=${encodeURIComponent(projectId)}`),
  gitSync: (name, projectId, ref) =>
    req(`${g(name)}/git-sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId, ref: ref || null }),
    }),
};
