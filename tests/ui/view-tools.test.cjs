// webui/view-tools.js — ✎ Disegna's tool table: register, select, keys, tabs,
// and the dispatcher that routes a gesture to the tool that started it.
// No DOM needed: the bar (mount) is the only part that touches the document.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'view-tools.js')).href);

function ev(type, id) {
  const e = new Event(type);
  Object.defineProperty(e, 'pointerId', { value: id });
  return e;
}

test('registering, selecting, keys and tabs', async () => {
  const T = await load();
  const log = [];
  const mk = (id, tab, extra = {}) => T.registerTool({ id, tab, icon: id, title: id,
    select: () => log.push('+' + id), deselect: () => log.push('-' + id), ...extra });
  mk('pen', 'pencil', { key: 'p' });
  mk('pen3d', 'pencil', { key: 'p' });
  mk('paint', 'pencil', { key: 't' });
  mk('tag', 'tag', { key: 't' });
  mk('erase', 'common', { key: 'e' });
  T.registerTool({ id: 'section', tab: 'tool', icon: '✂', title: 'in arrivo', disabled: true });
  assert.throws(() => T.registerTool({ id: 'pen', tab: 'pencil', icon: 'x', title: 'x' }), /duplicate/);
  assert.throws(() => T.registerTool({ id: 'zz', tab: 'nowhere', icon: 'x', title: 'x' }), /unknown tab/);

  assert.equal(T.byKey('p').id, 'pen');                 // nobody used one yet: the first
  assert.ok(T.select('pen3d'));
  assert.equal(T.byKey('p').id, 'pen3d');               // the one used last
  assert.equal(T.currentTab(), 'pencil');
  assert.ok(T.select('tag'));                           // a tool of another tab takes its tab
  assert.equal(T.currentTab(), 'tag');
  assert.deepEqual(log.slice(-2), ['-pen3d', '+tag']);
  assert.equal(T.select('section'), false);             // disabled: never in hand
  assert.equal(T.current().id, 'tag');

  T.setTab('pencil');                                   // back to the tool last used there
  assert.equal(T.current().id, 'pen3d');
  T.select('erase');                                    // a common tool stays in hand…
  T.setTab('tag');
  assert.equal(T.current().id, 'erase');                // …the tab only changes the row
  assert.equal(T.currentTab(), 'tag');
  T.preferKey('t', 'paint');                            // 't' already used (tag): kept
  assert.equal(T.byKey('t').id, 'tag');
});

test('a gesture stays with the tool that got its pointerdown', async () => {
  const T = await load();
  const got = [];
  for (const id of ['a1', 'b1']) T.registerTool({ id, tab: 'tool', icon: id, title: id,
    down: () => got.push(id + ':down'), move: () => got.push(id + ':move'), up: () => got.push(id + ':up') });
  const el = new EventTarget();
  T.attach(el);
  T.select('a1');
  el.dispatchEvent(ev('pointerdown', 7));
  T.select('b1');                                       // a key pressed mid-stroke
  el.dispatchEvent(ev('pointermove', 7));
  el.dispatchEvent(ev('pointerup', 7));
  el.dispatchEvent(ev('pointermove', 7));               // hover after: only the tool in hand
  assert.deepEqual(got, ['a1:down', 'b1:move', 'a1:move', 'b1:up', 'a1:up', 'b1:move']);
});
