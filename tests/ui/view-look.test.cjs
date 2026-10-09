// webui/view-look.js — 🔍 Aspetto in /view. The module needs three and a page,
// so only its pure part runs here: the `look=` link grammar (what an agent
// writes into a URL and what the page writes back).
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '../../webui/view-look.js'), 'utf8');
const cut = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const c = {};
vm.createContext(c);
vm.runInContext(cut('const OK_FINISH', '\n\nlet V') + '\n' +
  cut('export function parseLook', '\nexport function fromHash').replace('export ', ''), c);

test('finish and colour, in either order, per leaf key', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(c.parseLook('n3:glass,n7.2:emissive:#FFCC00,n5:ghost,n9:#ff0000'))), [
    ['n3', { finish: 'glass' }],
    ['n7.2', { finish: 'emissive', color: '#ffcc00' }],
    ['n5', { finish: 'ghost' }],
    ['n9', { color: '#ff0000' }],
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(c.parseLook('n1:%23aabbcc'.replace('%23', '#') + ':solid'))),
    [['n1', { color: '#aabbcc', finish: 'solid' }]]);
});
test('a colour without # is accepted, garbage is dropped, not thrown', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(c.parseLook('n1:ghost:00ff00'))), [['n1', { finish: 'ghost', color: '#00ff00' }]]);
  assert.deepEqual(JSON.parse(JSON.stringify(c.parseLook('n1:plasma,n2,,:glass,n3:#12'))), []);
  assert.deepEqual(JSON.parse(JSON.stringify(c.parseLook(''))), []);
  assert.deepEqual(JSON.parse(JSON.stringify(c.parseLook(null))), []);
});
