// ✂ Sezione in /view — the controls around webui/section.js.
//
// ONE state, two ways in (PLAN_VIEW_TOOLS §1-2): the ✂ tool in ✎ Disegna's
// 🔧 Tool tab (key X) for whoever is annotating, and a ✂ button beside
// Tutti / Inverti / Inquadra for whoever only wants to look. Both drive the
// same Section; the controls are built twice from one function and kept in step.
// Per piece, a ✂ in the list row leaves that piece WHOLE (the bolt intact in the
// cut nut: the classic assembly section). Hash: cut=z:12.5, cutflip=1,
// nocut=n3,n7.2 — read and written like hide=.
import { Section } from './section.js';
import { parseCut, formatCut, encodeKeys, decodeKeys } from './section-core.js';

const CSS = `
  .cutc{display:inline-flex;align-items:center;gap:4px;flex-wrap:nowrap;}
  .cutc .seg{display:inline-flex;}
  .cutc .seg .btn{min-width:30px;padding:0 6px;border-radius:0;}
  .cutc .seg .btn:first-child{border-radius:8px 0 0 8px;}
  .cutc .seg .btn:last-child{border-radius:0 8px 8px 0;}
  .cutc .seg .btn.on,.cutc .cut-flip.on,.cutc .cut-on.on{border-color:var(--accent);color:var(--accent);}
  .cutc input[type=range]{width:150px;accent-color:var(--accent);margin:0;touch-action:none;}
  .cutc .cut-val{font:11px var(--mono);color:var(--text-dim);min-width:64px;text-align:right;white-space:nowrap;}
  .cutc.off .seg,.cutc.off input,.cutc.off .cut-val,.cutc.off .cut-flip{opacity:.4;}
  #cutbar{position:absolute;top:10px;left:50%;transform:translateX(-50%);display:none;z-index:5;
    background:rgba(15,19,24,.9);border:1px solid var(--border);border-radius:12px;padding:5px 6px 5px 10px;
    box-shadow:0 8px 24px rgba(0,0,0,.5);max-width:calc(100% - 20px);}
  #cutbar.open{display:flex;align-items:center;gap:6px;}
  #cutbar .ttl{font-size:13px;}
  #vp.drawing #cutbar{display:none;}
  #b-cut.on{border-color:var(--accent);color:var(--accent);}
  .row .cut{font-size:12px;opacity:.9;}
  .row .cut.ex{opacity:.35;text-decoration:line-through;}
  .row .cut[hidden]{display:none;}
  @media (max-width:760px){
    #cutbar{top:30px;left:8px;right:8px;transform:none;padding:4px 6px;}
    #cutbar .ttl{display:none;}
    #cutbar .cutc{flex:1;min-width:0;}
    #cutbar .cutc input[type=range]{flex:1;width:auto;min-width:60px;}
    #cutbar .cutc .btn{min-width:34px;padding:0 4px;}
    #cutbar .cut-val{min-width:0;font-size:10px;}
  }
`;

// api: { viewer, $, vp, isDrawing(), boxOf() → Box3 of the SHOWN pieces,
//        nodes() → [{id, leaves:[{key}]}], changed() (list + hash) }
export function initSection(api) {
  const { viewer, $ } = api;
  const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
  const ui = [];                                   // every copy of the controls
  const sec = new Section(viewer, { onChange: () => { syncUI(); api.changed(); } });
  let placed = false;                              // has the plane been put somewhere yet?

  const centre = axis => {
    const b = api.boxOf(); if (!b || b.isEmpty()) return 0;
    const r = sec.range(b), i = 'xyz'.indexOf(axis);
    const c = (b.min.getComponent(i) + b.max.getComponent(i)) / 2;
    return +(Math.round(c / r.step) * r.step).toFixed(6);
  };
  // a new plane takes away the half facing the camera, so the cut faces you
  const facing = (axis, pos) => viewer.camera.position.getComponent('xyz'.indexOf(axis)) < pos;
  const fresh = axis => { const pos = centre(axis); return { on: true, axis, pos, flip: facing(axis, pos) }; };
  function turn(on) {
    if (on && !placed) { placed = true; sec.set(fresh(sec.state.axis)); }
    else sec.set({ on: !!on });
  }
  const toggle = () => turn(!sec.on);

  function controls({ close = false, onoff = false } = {}) {
    const el = document.createElement('span');
    el.className = 'cutc';
    el.innerHTML = `${onoff ? '<button class="btn cut-on" title="Accendi / spegni il taglio (X)">acceso</button>' : ''}
      <span class="seg" role="group" aria-label="Asse del piano di taglio">
        <button class="btn" data-ax="x" title="Piano normale a X">X</button><button class="btn" data-ax="y" title="Piano normale a Y">Y</button><button class="btn" data-ax="z" title="Piano normale a Z">Z</button></span>
      <input type="range" class="cut-pos" aria-label="Posizione del piano di taglio" title="Posizione del piano (dentro i pezzi visibili)">
      <span class="cut-val"></span>
      <button class="btn cut-flip" title="Inverti il lato tenuto">⇄</button>
      ${close ? '<button class="btn cut-x" aria-label="Togli il taglio" title="Togli il taglio (X)">✕</button>' : ''}`;
    const q = s => el.querySelector(s);
    for (const b of el.querySelectorAll('[data-ax]'))
      b.onclick = () => { placed = true; sec.set(fresh(b.dataset.ax)); };
    q('.cut-pos').addEventListener('input', e => { placed = true; sec.set({ on: true, pos: +e.target.value }); });
    q('.cut-flip').onclick = () => sec.set({ on: true, flip: !sec.state.flip });
    if (close) q('.cut-x').onclick = () => turn(false);
    if (onoff) q('.cut-on').onclick = toggle;
    ui.push(el);
    return el;
  }
  function syncUI() {
    const s = sec.state, r = sec.range(api.boxOf());
    for (const el of ui) {
      el.classList.toggle('off', !s.on);
      for (const b of el.querySelectorAll('[data-ax]')) b.classList.toggle('on', s.on && b.dataset.ax === s.axis);
      const R = el.querySelector('.cut-pos');
      R.min = Math.min(r.min, s.pos); R.max = Math.max(r.max, s.pos); R.step = r.step;
      if (document.activeElement !== R) R.value = s.pos;
      el.querySelector('.cut-val').textContent = `${s.axis.toUpperCase()} ${(+s.pos.toFixed(3))} mm`;
      el.querySelector('.cut-flip').classList.toggle('on', s.flip);
      const o = el.querySelector('.cut-on');
      if (o) { o.classList.toggle('on', s.on); o.textContent = s.on ? 'acceso' : 'spento'; }
    }
    $('cutbar').classList.toggle('open', s.on);
    $('b-cut').classList.toggle('on', s.on);
    for (const sp of document.querySelectorAll('.row .cut')) paintRowIcon(sp);
  }

  // the view bar: ✂ beside Inquadra, and its floating controls
  const btn = document.createElement('button');
  btn.className = 'btn'; btn.id = 'b-cut';
  btn.title = 'Sezione: taglia i pezzi con un piano e mostra la faccia di taglio campita (X)';
  btn.innerHTML = '<span class="ic">✂</span>Sezione';
  btn.onclick = toggle;
  $('b-frame').after(btn);
  const bar = document.createElement('div');
  bar.id = 'cutbar';
  bar.innerHTML = '<span class="ttl">✂</span>';
  bar.appendChild(controls({ close: true }));
  api.vp.appendChild(bar);

  // the ✎ Disegna tool (🔧 Tool tab): taking it turns the cut on; it has no
  // gesture of its own, so the view keeps orbiting and taps keep selecting
  const tool = {
    id: 'section', tab: 'tool', key: 'x', icon: '✂', label: 'Sezione',
    title: 'Sezione: taglia i pezzi con un piano X/Y/Z — la faccia di taglio campita a righe; nella lista ✂ lascia intero un pezzo',
    hint: () => sec.on ? 'Sezione: sposta il piano col cursore · ⇄ inverte il lato · ✂ nella lista lascia intero un pezzo · si disegna anche sul taglio'
                       : 'Sezione: spenta — «acceso» o un asse la accende',
    options: () => controls({ onoff: true }),
    select: () => { if (api.isDrawing()) turn(true); },
  };

  // per piece: the ✂ in its list row (shown while a cut is on)
  function paintRowIcon(sp) {
    const keys = sp._keys || [];
    const ex = keys.length && keys.every(k => sec.nocut.has(k));
    sp.hidden = !sec.on;
    sp.classList.toggle('ex', !!ex);
    sp.title = ex ? 'Escluso dal taglio: resta intero — tocca per tagliarlo' : 'Tagliato — tocca per lasciarlo intero';
    sp.setAttribute('aria-pressed', ex ? 'true' : 'false');
  }
  function decorate(row, keys) {
    const sp = document.createElement('span');
    sp.className = 'ib cut'; sp.setAttribute('role', 'button'); sp.textContent = '✂';
    sp.setAttribute('aria-label', 'Escludi dal taglio');
    sp._keys = keys;
    sp.addEventListener('click', e => {
      e.stopPropagation();
      sec.setExcluded(keys, !keys.every(k => sec.nocut.has(k)));
    });
    paintRowIcon(sp);
    row.querySelector('.solo').before(sp);
  }

  const plain = () => api.nodes().map(n => ({ id: n.id, leaves: n.leaves.map(l => l.key) }));
  function hashParts() {
    if (!sec.on) return [];
    const ps = ['cut=' + formatCut(sec.state)];
    if (sec.state.flip) ps.push('cutflip=1');
    const nc = encodeKeys(sec.nocut, plain());
    if (nc) ps.push('nocut=' + nc);
    return ps;
  }
  function readHash(param) {
    const c = parseCut(param('cut'));
    for (const k of decodeKeys(param('nocut'), plain())) sec.nocut.add(k);
    if (c) { placed = true; sec.set({ on: true, axis: c.axis, pos: c.pos, flip: param('cutflip') === '1' }); }
  }
  // what a note saved on a sectioned view carries
  const noteCut = () => sec.on ? sec.toJSON() : null;

  syncUI();
  window.__noodleSection = sec;                   // for the headless probes
  return { sec, tool, toggle, decorate, hashParts, readHash, noteCut, sync: syncUI,
           bind: leaves => { sec.bind(leaves); syncUI(); } };
}
