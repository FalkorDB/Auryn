// Per-category colour + visibility, persisted per-graph in localStorage so a
// user's choices survive reloads. Colours are assigned from a curated
// categorical set in first-seen order (hashing label names into a small palette
// collides badly — e.g. "Table"/"Column" landing on the same hue).

// A categorical palette tuned to read clearly on the near-black canvas:
// distinct hues, similar luminance, none of them muddy.
const DEFAULTS = [
  "#6ea8fe", // blue
  "#f2a65a", // amber
  "#5fd0a6", // mint
  "#e56b8c", // rose
  "#b990f5", // violet
  "#56c9d6", // cyan
  "#d9c46a", // gold
  "#8fbf6a", // leaf
  "#ef8b6a", // coral
  "#7f9cf5", // periwinkle
  "#d98ad6", // orchid
  "#6ac2b3", // teal
  "#c98f7a", // clay
  "#9aa7ff", // iris
];

const FALLBACK = "#8b93a1";

export class Palette {
  constructor() {
    this.graphName = null;
    this.colors = {};      // label -> hex
    this.hidden = new Set(); // labels toggled off
    this._order = [];      // first-seen label order (drives default assignment)
    this._listeners = new Set();
  }

  onChange(fn) { this._listeners.add(fn); return () => this._listeners.delete(fn); }
  _emit() { for (const fn of this._listeners) fn(); }

  _key() { return `auryn.palette.${this.graphName}`; }

  // Load persisted overrides for a graph. Only *colours* persist — they're a
  // deliberate preference. Visibility is a transient exploration filter, so it
  // always starts with everything shown (persisting it caused fresh loads to
  // come up mostly-hidden after a bit of toggling).
  load(graphName, labels) {
    this.graphName = graphName;
    this.colors = {};
    this.hidden = new Set();
    this._order = [];
    for (const label of labels) this._ensure(label);
    try {
      const saved = JSON.parse(localStorage.getItem(this._key()) || "{}");
      if (saved.colors) Object.assign(this.colors, saved.colors);
    } catch { /* ignore corrupt storage */ }
    this._emit();
  }

  _persist() {
    if (!this.graphName) return;
    try {
      localStorage.setItem(this._key(), JSON.stringify({ colors: this.colors }));
    } catch { /* storage full / disabled — non-fatal */ }
  }

  _ensure(label) {
    if (label == null) return;
    if (!(label in this.colors)) {
      this.colors[label] = DEFAULTS[this._order.length % DEFAULTS.length];
      this._order.push(label);
    }
  }

  colorFor(label) {
    if (label == null) return FALLBACK;
    this._ensure(label);
    return this.colors[label] || FALLBACK;
  }

  setColor(label, hex) { this._ensure(label); this.colors[label] = hex; this._persist(); this._emit(); }

  isVisible(label) { return !this.hidden.has(label); }
  setVisible(label, visible) {
    if (visible) this.hidden.delete(label); else this.hidden.add(label);
    this._persist(); this._emit();
  }
  toggle(label) { this.setVisible(label, !this.isVisible(label)); }

  showAll() { this.hidden.clear(); this._persist(); this._emit(); }
  hideAll(labels) { this.hidden = new Set(labels); this._persist(); this._emit(); }

  resetColors() {
    this.colors = {};
    const order = this._order.slice();
    this._order = [];
    for (const label of order) this._ensure(label);
    this._persist(); this._emit();
  }
}
