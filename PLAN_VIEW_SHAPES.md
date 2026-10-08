# ▣ Forme, giro 2 — colore, gizmo a scelta, gabbia che deforma

> Piano per una sessione nuova, scritto il 2026-10-08 alla fine di quella che
> ha fatto ↔ Metro e ▣ Forme (PR rederyk/noodle#28, branch
> `feat/view-measure`). Le richieste sono di quill, parola per parola in fondo.
> Il lavoro è diviso in compiti per **subagenti**, con le dipendenze e le
> regole per non pestarsi i piedi su un file solo (`webui/view.html`).

## Da dove si parte

- Branch di partenza: `plan/view-shapes` (= `feat/view-measure` + questo file).
  Se la #28 è già mergiata, ripartire da `origin/main` e cherry-pickare questo
  file. **Controllare prima**: `gh pr view 28 -R rederyk/noodle --json state`.
- Lo stato attuale delle forme è in CLAUDE.md §9c, punto «▣ Forme». In breve:
  un tocco posa cubo / cilindro / sfera appoggiati alla superficie; selezionata
  ha una gabbia con 8 vertici (ridimensionano, vertice opposto fermo, si
  fermano al minimo = 2% del pezzo), un pallino al centro (scala attorno alla
  base), 3 anelli (ruotano, Shift 15°) e 3 frecce X/Y/Z (spostano, al
  guinzaglio entro l'ingombro del pezzo + ¼). Trascinare il corpo la fa
  scivolare incollata sui pezzi. Il corpo è carta millimetrata nel suo colore.
- Codice, tutto in `webui/view.html`, blocco `// ── ▣ Forme` (~righe 1660–2050):
  `shapeObject`, `mmGridMaterial`, `drawShape`, `shapeData`, `shapeFrom`,
  `shapeContains`, `shapeMin`, `shapeLeash`, `addShape`, `refreshCage`
  (costruisce gabbia, vertici, centro, anelli, frecce), `selectShape`,
  `handleAt` (sceglie la maniglia: la più vicina sullo schermo; sul corpo una
  maniglia vince solo entro metà raggio di presa), i tre listener
  `pointerdown` / `pointermove` / `endShapeDrag` con `sDrag.h.type` =
  `move | corner | center | ring | arrow`, la barra `#s-bar` (dimensioni,
  Togli, ✓). I colori della penna: `PEN_COLORS`, il ciclo
  `for (const c of PEN_COLORS)` (~riga 2778) e `#d-custom`.
- Backend: `cad_nodes/api.py` `_shapes` (validazione della nota),
  `shape_phrase`, `_link_shapes` (summary per `cad_notes`). Test:
  `tests/test_notes.py` (cercare `SHAPE =` e i test `test_the_shape_tool_*`,
  `test_shapes_*`).

## Le decisioni già prese (non rimetterle in discussione)

- Spostare col corpo resta «incollato ai pezzi»; le frecce restano al guinzaglio.
- Il minimo di scala segue il pezzo (2%); i vertici non si toccano né si
  attraversano; i segni delle maniglie scalano con la forma, la presa no.
- Le forme sono dati per l'agente: ogni cambiamento di aspetto o di forma deve
  arrivare nella nota (`shapes`) e in `cad_notes`, non solo a schermo.

## I compiti

Quattro subagenti di lavoro + uno di integrazione. Ognuno lavora in una
**worktree sua**, su un branch suo che parte da `plan/view-shapes`
(`git worktree add ../noodle-shapes-<x> -b feat/shapes-<x> plan/view-shapes`).
L'orchestratore li integra in ordine. Dipendenze:

```
fase 1 (in parallelo):  A colore            B barra dei modi (gizmo)
fase 2 (dopo B):        C gabbia che deforma   D scala con colori XYZ
fase 3:                 E integrazione, prove nel browser, deploy, PR
```

Perché così: B introduce lo stato «quale gizmo» che C e D usano; A tocca solo
i colori. C e D toccano zone diverse di `refreshCage` e del `pointermove`:
ognuno aggiunge il PROPRIO ramo (`case`) e non riscrive quelli degli altri.

### A — Ricolorare una forma con i colori della penna

- Con una forma selezionata (`selShape`), un clic su un colore della penna
  (`PEN_COLORS` o `#d-custom`) **ricolora quella forma** invece di cambiare
  strumento. Senza selezione resta com'è oggi (cambia il colore della penna).
- Anche `pen.color` si aggiorna, così la prossima forma nasce di quel colore.
- È un'azione annullabile: `actions.push({ type: 'shape', S, before })` —
  serve che `shapeState` / `setShapeState` portino anche `color` (oggi no).
- `drawShape(S)` ridisegna già il materiale con `S.color`; il bordo
  (`LineSegments`) e la griglia millimetrata seguono.
- Test: `tests/test_notes.py` — stringhe che fissano il ramo «selShape →
  ricolora» e che `shapeState` includa il colore.
- Prova nel browser: posa un cubo rosso, selezionalo, clicca il blu → il cubo
  è blu, ↶ lo riporta rosso, la nota inviata ha `color: "#3b82f6"`.

### B — Barra dei modi del gizmo: Sposta / Ruota / Gabbia / Nascondi

Oggi frecce e anelli ci sono sempre, insieme. quill vuole sceglierli.

- In `#s-bar` (o in una riga accanto) quattro bottoni a scelta singola /
  interruttore:
  - **Sposta** — mostra solo le frecce X/Y/Z;
  - **Ruota** — mostra solo gli anelli;
  - **Gabbia** — mostra solo vertici + centro; un secondo tocco sullo stesso
    bottone (o un interruttore accanto) passa fra **Scala** e **Deforma**
    (il ramo Deforma lo implementa C; finché C non c'è, il bottone è disabilitato
    o resta su Scala);
  - **Nascondi gizmo** — niente maniglie: si vede la forma e basta (si può
    ancora trascinare il corpo per farla scivolare sui pezzi).
- Lo stato vive in una variabile (`gizmoMode = 'move'|'rotate'|'cage'|'none'`,
  `cageMode = 'scale'|'deform'`), ricordata in localStorage
  (`noodle:view:gizmoMode`, `noodle:view:cageMode`), con try/catch come gli
  altri. `refreshCage()` costruisce SOLO le maniglie del modo attivo.
  `handleAt` non cambia: trova solo ciò che c'è.
- Tasti: `G` sposta, `R` ruota, `C` gabbia, `H` nascondi — attenzione: `H`
  è già «nascondi pezzo» fuori dal disegno e `R`/`C` non devono collidere con
  la tastiera di ✎ Disegna (vedi il listener `keydown` del blocco). Se
  collidono, niente tasti: i bottoni bastano.
- Telefono: la barra deve stare in una riga sola a 390 px (vedi la regola
  `#dbar .grp{flex-wrap:wrap}` e perché c'è: 604 px di strumenti facevano
  scorrere tutto il viewer di lato). Bottoni con icona + etichetta corta.
- Test: stringhe che fissano i quattro modi e che `refreshCage` legga
  `gizmoMode`.

### C — La gabbia che DEFORMA davvero (FFD)

Oggi i vertici ridimensionano una scatola. In modo **Deforma** ogni vertice si
muove da solo e la forma si deforma con lui: una deformazione trilineare
(free-form deformation a 8 punti di controllo).

- Dato: ogni forma porta 8 spostamenti dei vertici nel SUO riferimento
  locale normalizzato, `S.ffd = [[dx,dy,dz] × 8]` (0 = cubo unitario). Ordine
  dei vertici: lo stesso di `refreshCage` (`sx, sy, sz ∈ {−1, 1}`, indice
  `i` in ordine di ciclo).
- Geometria: i vertici della geometria unitaria (`shapeGeo(kind)`, in
  [−0.5, 0.5]³) vengono spostati in CPU con l'interpolazione trilineare degli
  8 spostamenti, ad ogni `drawShape`. Usare una COPIA della geometria (non
  quella in cache) e ricalcolare le normali. Il bordo (`EdgesGeometry`) va
  ricalcolato sulla geometria deformata.
- La griglia millimetrata: `mmGridMaterial` usa `position * uSize`, cioè la
  posizione NON deformata → la griglia segue la deformazione come una texture.
  È il comportamento giusto (si vede la deformazione); va passata la
  posizione di riposo come attributo a parte (`restPos`) se il vertex shader
  riceve quella deformata.
- Trascinamento di un vertice in Deforma: sul piano vista per il vertice
  (`onViewPlane`), lo spostamento diventa locale con
  `applyQuaternion(q.invert())` e si divide per `S.size`. **Vincoli** (stessi
  della scala): i vertici non si toccano e non si attraversano — ogni vertice
  resta nel proprio ottante rispetto al centro della gabbia, a una distanza
  minima dagli altri = `shapeMin(S)`.
- Le maniglie in Deforma: stesso cubetto, colore diverso (per esempio ambra),
  e le linee della gabbia seguono i vertici spostati.
- Nota per l'agente: `shapeData` aggiunge `ffd` (8 terne) e `corners` (gli 8
  vertici in coordinate mondo, mm) — un modello solo-testo deve poter capire
  la forma senza fare l'interpolazione. Backend: `_shapes` valida `ffd`
  (8 × 3 numeri finiti, |d| < 10) e `corners` (8 punti); `_link_shapes`
  scrive «deformed» nel summary quando `ffd` non è nullo. MCP `cad_notes` e
  AGENT_HELP: una riga su `ffd` / `corners`.
- `shapeContains` (gomma) e `shapeLeash` usano il box dei `corners` invece
  di `size` quando la forma è deformata.
- Test: puri — la trilineare (una funzione esportabile, magari in
  `webui/measure.js` o in un `webui/ffd.js` puro, testato in node come
  `tests/ui/measure.test.cjs`): con ffd nullo la geometria è identica; un
  vertice spostato muove solo il suo ottante; i vincoli tengono. Backend:
  `_shapes` accetta/rifiuta.

### D — Modo Scala con i colori XYZ classici

In modo **Gabbia → Scala** le maniglie prendono i colori classici degli assi:

- Sei maniglie al centro delle facce della gabbia: ±X rosse, ±Y verdi,
  ±Z blu (gli stessi `RING_INK`): trascinarne una scala **solo quell'asse**,
  con la faccia opposta ferma (come oggi i vertici, ma su un asse).
- Gli 8 vertici restano (scala libera, vertice opposto fermo) in bianco; il
  centro resta la scala uniforme.
- Per il cilindro ±X e ±Y scalano insieme il diametro (il cilindro resta
  tondo); per la sfera tutte e sei scalano uniformemente — come fanno già i
  vertici (`if (S.kind === 'sphere')` / `'cylinder'` nel ramo `corner`).
- Minimo: `shapeMin(S)` per asse, mai oltre la faccia opposta (stesso calcolo
  con segno del ramo `corner`).
- Test: stringhe che fissano le sei maniglie per asse e il loro ramo.

### E — Integrazione, prove, deploy, PR (orchestratore o subagente finale)

1. Integrare in ordine A, B, poi C e D (merge o cherry-pick dei branch dei
   subagenti in `feat/view-shapes`); risolvere a mano i conflitti in
   `view.html`.
2. Test: `nix develop ~/nixos/shells#noodle -c python -m pytest tests/ -q` e
   `node --test tests/ui/measure.test.cjs tests/ui/anticipate.test.cjs` (node
   vuole i FILE, non la cartella).
3. **Prove nel browser vero**, desktop 1280×800 e telefono 390×780 (sotto lo
   scheletro dello script). Guardare gli screenshot, non solo i numeri.
4. CLAUDE.md §9c, punto «▣ Forme»: aggiornarlo.
5. Deploy sul gpunix e aggiornamento della PR (vedi «Come si lavora qui»).

## Come si lavora qui (lezioni pagate)

- Il working tree `~/projects/noodle` è condiviso con altri agenti: mai
  cambiarci branch, mai `git add` di file non propri. Si lavora in worktree.
- **Container di prova**, mai il quadlet `noodle` (serve il working tree
  condiviso): `podman run -d --name noodle-<x> --userns=keep-id:uid=1000,gid=1000
  -p 127.0.0.1:809N:8090 -v <scratch>/projects:/app/projects -v <scratch>/feedback:/app/feedback
  -v <worktree>/cad_nodes:/app/cad_nodes:ro -v <worktree>/webui:/app/webui:ro
  -v <worktree>/server.py:/app/server.py:ro -v <worktree>/mcp_server.py:/app/mcp_server.py:ro
  localhost/noodle_noodle:latest`. Dopo modifiche backend `docker restart`.
  Progetti di prova (copiare in `<scratch>/projects` da `~/projects/noodle/projects`):
  `zz-note-probe` (bullone e dado, MESH) e un B-Rep semplice (Box + Cylinder
  spostato: crearlo con `POST /api/graph/<nome>` e `POST …/snapshot`).
- Ganci di prova nella pagina: `window.__noodleMeasures` (`viewer`, `draw`,
  `list`, `pending`, `draft`, `probe`, `handleAt`, `shapeAt`). La forma
  selezionata si legge da
  `viewer.scene.getObjectByName('notes').children.filter(o => o.userData.shape)`
  e le maniglie da `viewer.scene.getObjectByName('shape-cage').children`
  (`userData.handle`). Leggere le posizioni VERE delle maniglie da lì, non
  indovinarle: metà delle prove fallite della sessione precedente erano
  clic su punti sbagliati (un vertice dietro la forma a 1,9 px, un punto sul
  bordo della silhouette, un bersaglio sotto la barra del telefono).
- Scheletro Playwright (gira DENTRO il container: `docker cp` + `docker exec
  noodle-<x> python /tmp/x.py`):

  ```python
  import asyncio
  from playwright.async_api import async_playwright
  V = "window.__noodleMeasures.viewer"
  async def main():
      async with async_playwright() as p:
          b = await p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
          pg = await b.new_page(viewport={"width": 1280, "height": 800})
          pg.on("pageerror", lambda e: print("pageerror:", e))
          await pg.goto("http://localhost:8090/view/<progetto>/g1#measures=0")
          await pg.wait_for_function("window.__noodleMeasures && window.__viewBooted", timeout=60000)
          await pg.evaluate(f"(([p, t]) => {{ const V = {V}; V.camera.position.set(...p); V.controls.target.set(...t); V.controls.update(); V.invalidate(); }})", [[14, -48, 40], [12, 0, 0]])
          proj = lambda pt: pg.evaluate(f"(p) => {{ const V = {V}, c = V.renderer.domElement.getBoundingClientRect(); const v = new V.camera.position.constructor(...p).project(V.camera); return [c.left + (v.x + 1) / 2 * c.width, c.top + (1 - v.y) / 2 * c.height]; }}", pt)
          await pg.click("#d-open"); await pg.click("#d-shape")
          x, y = await proj([0, 0, 5]); await pg.mouse.click(x, y)
          await pg.screenshot(path="/tmp/out/x.png")
          await b.close()
  asyncio.run(main())
  ```

  Telefono: `b.new_context(viewport={"width": 390, "height": 780},
  device_scale_factor=2, is_mobile=True, has_touch=True)` e tocchi con CDP
  `Input.dispatchTouchEvent`. Controllare `getBoundingClientRect()` del canvas:
  `left < 0` = la pagina è scivolata di lato.
- **Deploy**: il gpunix (`ssh -i ~/.ssh/quillAgent reder@gpunix.lan`,
  `~/services/noodle`) gira oggi `feat/view-measure` (tracking
  `fork/feat/view-measure`). Per il nuovo lavoro: push del branch sul fork,
  sul gpunix `git fetch fork && git checkout <branch>`, e
  `systemctl --user restart noodle` solo se cambia il backend (`cad_nodes/`,
  `server.py`, `mcp_server.py`); il frontend basta ricaricarlo. Il server MCP
  `noodle` delle sessioni si scollega a ogni riavvio: `/mcp` per ricollegarlo.
- **PR**: da `quill4gen7:<branch>` verso `rederyk/noodle:main`, descrizione in
  inglese con gli screenshot in `docs/asset/` (link raw.githubusercontent del
  fork). Se la #28 è ancora aperta, valutare con quill se accodare a lei o
  aprirne una nuova sopra.

## Ancora aperto dal giro del Metro (non in questo piano, per memoria)

- Quota d'angolo (v2).
- Targa delle quote a dimensione fissa sullo schermo invece che in mm.
- `between` per pezzi della corsia mesh (oggi `measure.py distance` vuole un B-Rep).
- I ganci di prova `probe` / `pending` / `draft` in `window.__noodleMeasures`
  sono utili alle prove ma esposti a tutti: decidere se tenerli.

## Le richieste di quill (2026-10-08, testuali)

> permetti di cambiare colore al pezzo con i colori dei pennelli e metti i
> bottoni per nascondere cambiare move invece di averli sempre entrambi e
> nascondere gyzmo, e fai un bottone anche per la gabbia falla davvero di
> deform e il bottone usalo per switchare da scale a deform in scale metti i
> colori xyz classici, scrivi i task per una sessione nuova e dividili per
> subagenti
