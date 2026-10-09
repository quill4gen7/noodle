// ✎ Disegna's tools — a table and a dispatcher, not a framework.
//
// The draw bar of /view (webui/view.html) used to be one row of every tool,
// and every tool was spread over five places of a 4000-line page: the bar's
// HTML, a `#vp[data-tool=…]` cursor rule, the key switch, the pointer
// listeners and the "which button is on" list. A new tool now is ONE call:
//
//   import { registerTool, ctx } from '/static/view-tools.js';
//   registerTool({ id: 'section', tab: 'tool', icon: '✂', label: 'Sezione',
//     title: 'Sezione: …', key: 'x', cursor: 'crosshair', hint: 'Tocca …',
//     options: () => mySelect,                 // the tool's own controls, beside its button
//     select() {}, deselect() {},              // taking it in hand / putting it down
//     down(e) {}, move(e) {}, up(e) {}, cancel(e) {},  // the gesture (capture phase on #vp)
//   });
//
// Tabs (TABS): ✎ Matita · ◆ Tag · ▣ Blocky · 🔧 Tool, keys 1-4. A tool with
// `tab: 'common'` sits in the row every tab shares (⌫ gomma, Muovi). Each tab
// remembers the tool last used in it, and a key shared by several tools (P: ✎ or
// ✎³, T: vernice / decal / targhetta) takes the one last used — all kept in
// localStorage (a per-viewer convenience, never needed).
//
// The pointer handlers run in the CAPTURE phase on the viewport, BEFORE
// OrbitControls: a handler that takes a press calls e.stopPropagation() (and
// usually setPointerCapture); one that leaves it alone lets the view orbit. A
// gesture keeps going to the tool that got its pointerdown even if the tool
// changes under it (a key pressed mid-stroke must not strand the stroke).
// Handlers run only while ✎ Disegna is open.
//
// `ctx` is what view.html shares with a tool module (viewer, surfaceHit,
// pushAction, syncDraw, toast…) — filled at boot, read at call time.
// An action a module pushes may carry its own `undo()` / `redo()` (each
// returning the view indices it touched, for the pictures): ↶ ↷ call them.

export const TABS = [
  { id: 'pencil', icon: '✎', label: 'Matita', key: '1', title: 'Matita: penna, penna 3D, testo a vernice, decal' },
  { id: 'tag', icon: '◆', label: 'Tag', key: '2', title: 'Tag: targhette e immagini che indicano un punto' },
  { id: 'blocky', icon: '▣', label: 'Blocky', key: '3', title: 'Blocky: cubi, cilindri e sfere posati sul pezzo' },
  { id: 'tool', icon: '🔧', label: 'Tool', key: '4', title: 'Tool: metro e sezione' },
];
export const TOOLS = new Map();                // id → def, in registration order
export const ctx = {};

const STORE = 'noodle:view:drawTools';
const st = { tool: null, tab: TABS[0].id, byTab: {}, byKey: {}, dom: null, onChange: null, active: () => true };
try { Object.assign(st, JSON.parse(localStorage.getItem(STORE)) || {}); } catch {}
st.tool = null;                                // the TOOL is chosen at mount, from the tab
if (!TABS.some(t => t.id === st.tab)) st.tab = TABS[0].id;
function remember() {
  try { localStorage.setItem(STORE, JSON.stringify({ tab: st.tab, byTab: st.byTab, byKey: st.byKey })); } catch {}
}

export function registerTool(def) {
  if (!def || !def.id || TOOLS.has(def.id)) throw new Error(`registerTool: bad or duplicate id ${def && def.id}`);
  if (def.tab !== 'common' && !TABS.some(t => t.id === def.tab)) throw new Error(`registerTool ${def.id}: unknown tab ${def.tab}`);
  TOOLS.set(def.id, def);
  if (st.dom) mountTool(def);
  return def;
}
export const current = () => (st.tool && TOOLS.get(st.tool)) || null;
export const currentTab = () => st.tab;
export const toolsOf = tab => [...TOOLS.values()].filter(d => d.tab === tab);

// The tool to pick for a key: of the tools with that key, the one used last.
export function byKey(k) {
  const all = [...TOOLS.values()].filter(d => d.key === k && !d.disabled);
  if (!all.length) return null;
  return all.find(d => d.id === st.byKey[k]) || all[0];
}

export function select(id, { quiet = false } = {}) {
  const def = TOOLS.get(id);
  if (!def || def.disabled) return false;
  const prev = current();
  if (prev && prev !== def && prev.deselect) prev.deselect(def);
  st.tool = id;
  if (def.tab !== 'common') { st.tab = def.tab; st.byTab[def.tab] = id; }
  if (def.key) st.byKey[def.key] = id;
  remember();
  if (def.select && prev !== def) def.select(prev);
  sync();
  if (!quiet && st.onChange) st.onChange(def, prev);
  return true;
}
// a tab: back to the tool last used in it (or its first); a common tool in
// hand (⌫, Muovi) stays in hand — the tab only changes what the row offers
export function setTab(tab) {
  if (!TABS.some(t => t.id === tab)) return false;
  st.tab = tab; remember();
  const cur = current();
  if (cur && cur.tab === 'common') { sync(); return true; }
  const list = toolsOf(tab).filter(d => !d.disabled);
  const pick = list.find(d => d.id === st.byTab[tab]) || list[0];
  if (pick) select(pick.id); else sync();
  return true;
}

// ── the bar ──
// tabs: the tablist element; rows: where the tab rows go; common: the common
// row's slot for `tab: 'common'` tools. The first tool comes from the
// remembered tab.
export function mount({ tabs, rows, common, onChange, active }) {
  st.dom = { tabs, rows, common, rowOf: {}, btnOf: {} };
  st.onChange = onChange || null;
  if (active) st.active = active;
  for (const t of TABS) {
    const b = document.createElement('button');
    b.className = 'btn dtab'; b.id = 'dtab-' + t.id; b.dataset.tab = t.id;
    b.setAttribute('role', 'tab'); b.title = `${t.title} (${t.key})`;
    b.innerHTML = `<span class="ic">${t.icon}</span><span class="lb">${t.label}</span>`;
    b.onclick = () => setTab(t.id);
    tabs.appendChild(b);
    const row = document.createElement('div');
    row.className = 'trow'; row.dataset.tab = t.id; row.setAttribute('role', 'tabpanel');
    rows.appendChild(row);
    st.dom.rowOf[t.id] = row;
  }
  for (const def of TOOLS.values()) mountTool(def);
  const list = toolsOf(st.tab).filter(d => !d.disabled);
  const first = list.find(d => d.id === st.byTab[st.tab]) || list[0];
  if (first) select(first.id, { quiet: true }); else sync();
}
function mountTool(def) {
  const d = st.dom;
  const b = (def.el && document.getElementById(def.el)) || document.createElement('button');
  b.classList.add('btn', 'tool');
  if (!b.id) b.id = 'dt-' + def.id;
  b.dataset.tool = def.id;
  b.title = def.title + (def.key ? ` (${def.key.toUpperCase()})` : '');
  if (!b.innerHTML.trim()) b.innerHTML = `<span class="ic">${def.icon}</span>${def.label ? `<span class="lb">${def.label}</span>` : ''}`;
  b.disabled = !!def.disabled;
  b.onclick = () => (def.click ? def.click() : select(def.id));
  const host = def.tab === 'common' ? d.common : d.rowOf[def.tab];
  const wrap = document.createElement('span');
  wrap.className = 'titem'; wrap.dataset.tool = def.id;
  wrap.appendChild(b);
  if (def.options) for (const o of [].concat(def.options() || [])) if (o) { o.hidden = false; wrap.appendChild(o); }
  host.appendChild(wrap);
  d.btnOf[def.id] = b;
}
// which tab / tool is on — call after anything that may change it
export function sync() {
  const d = st.dom; if (!d) return;
  for (const b of d.tabs.children) {
    const on = b.dataset.tab === st.tab;
    b.classList.toggle('on', on); b.setAttribute('aria-selected', on);
  }
  for (const [tab, row] of Object.entries(d.rowOf)) row.hidden = tab !== st.tab;
  for (const [id, b] of Object.entries(d.btnOf)) {
    b.classList.toggle('on', id === st.tool);
    b.disabled = !!TOOLS.get(id).disabled;
  }
}

// ── the dispatcher ──
const HANDLER = { pointerdown: 'down', pointermove: 'move', pointerup: 'up', pointercancel: 'cancel', pointerleave: 'leave' };
const owner = new Map();                       // pointerId → the tool that got its pointerdown
export function attach(el) {
  for (const [type, h] of Object.entries(HANDLER)) {
    el.addEventListener(type, e => {
      if (!st.active()) return;
      const cur = current();
      const targets = [cur];
      if (type === 'pointerdown') owner.set(e.pointerId, cur);
      else { const o = owner.get(e.pointerId); if (o && o !== cur) targets.push(o); }
      if (type === 'pointerup' || type === 'pointercancel') owner.delete(e.pointerId);
      for (const t of targets) if (t && t[h]) t[h](e);
    }, true);
  }
}
// a default for a shared key, unless the viewer already used one of its tools
export function preferKey(k, id) { if (!st.byKey[k] && TOOLS.has(id)) st.byKey[k] = id; }
