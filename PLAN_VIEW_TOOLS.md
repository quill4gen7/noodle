# /view — strumenti in tab, vedere dentro, targhe che non coprono

Piano, non ancora codice. Richiesta di quill (2026-10-09), più i segnali che
le sue note degli ultimi giorni lasciano già nei dati. Raccoglie e mette in
ordine anche `PLAN_VIEW_SECTION.md` (✂ Sezione + 🔍 Aspetto, solo piano
dall'8/10): lì il COME è già studiato e misurato, qui si decide il DOVE e
l'ordine.

Base: `plan/view-section` (3c527d4) = tutta la pila non mergiata
`feat/view-measure` (PR #28) → `feat/view-shapes` → `feat/view-pen3d` →
`fix/feedback-20261008-153010-creepyfinger-v4` + il piano della sezione.
Tutto il lavoro qui parte da lì.

---

## 0. Cosa chiede quill, e cosa dicono già i dati

La richiesta, in sei punti:

1. **colore e soprattutto materiale ai pezzi** — trasparente o luminoso, per
   vedere meglio (= `PLAN_VIEW_SECTION` §2, 🔍 Aspetto);
2. **✂ sezione** che taglia i pezzi con la classica campitura a righe
   (= `PLAN_VIEW_SECTION` §1);
3. **targhe e tag troppo davanti**: quando cerchi di guardare il punto da
   indicare, la targhetta lo copre. Ingrandirle/ridurle, farle diventare
   traslucide, nasconderle quando si zooma sulla cosa che indicano;
4. **✎ Disegna diventa una vera barra degli strumenti a tab**:
   - **✎ Matita** — penna, testo incollato (vernice/decal), penna 3D, e un
     modo «alla ZBrush» per disegnare su PIANI anche dove il pezzo non c'è;
   - **◆ Tag** — tutto ciò che indica un punto preciso e lo etichetta con
     qualcosa di standard: targhette, ↔ metro, Img, la nota;
   - **▣ Blocky** — le forme: posarle, trasformarle, modellarle (gabbia FFD);
   - **una riga comune** sempre visibile: ↶ undo, ↷ redo, ⌫ gomma (+ colore,
     misura, chiudi);
5. **niente più campo nota** nella barra: la nota diventa un TAG, e appare
   come **nota flottante di tutto il progetto**;
6. (implicito) tutto resta salvato da solo, come dal fix del 20261008.

Cosa dicono le note vere (`cad_notes`, creepyfinger-v4, 9/10 notte) — a
supporto, non da aggiungere:

- **Il punto 3 è già un comportamento**: g49#a1, g49#a2, g51#a1, g51#a2 sono
  state disegnate con `tags=0` nell'hash — quill spegne TUTTE le etichette
  dell'agente per poter vedere dove disegnare. Oggi l'unico rimedio è
  tutto-o-niente.
- **Il punto 5 pure**: g51#a2 è fatta solo di targhette (testo nota vuoto);
  g57#a2, g58#a1, g63#a1, g63#a3 hanno il testo vuoto. Quill scrive DOVE,
  non nel campo in basso. g64#a1: una targhetta da 29,9 × 8 mm su un pezzo
  di ~25 mm di spessore — una targa più grande del dettaglio che indica.
- Le note aperte (g62#a1, g58#a1/a2, g57#a2, g63#a3, g47#a4) riguardano il
  MODELLO, non lo strumento: fuori da questo piano.

---

## 1. La nuova barra

```
┌ [✎ Matita] [◆ Tag] [▣ Blocky] ─────────── ↶ ↷ ⌫ │ ● ● ● ● ◐ │ · • ● │ 💾 ✕ ┐
│  (riga dello strumento: cambia col tab)                                     │
│  Matita: ✎ penna · ✎³ penna 3D · T vernice · ▭ decal · ⊞ piano [Sup|XY|XZ|YZ|Vista] │
│  Tag:    ⚑ targhetta · ↔ metro [modo] · 🖼 img · 📝 nota                     │
│  Blocky: ▣ cubo/cilindro/sfera · ✥ ⟳ ▣gabbia (Scala|Deforma) ◌              │
└─────────────────────────────────────────────────────────────────────────────┘
```

- La riga comune è la stessa in tutti i tab: ↶/↷, ⌫ gomma (cancella
  QUALUNQUE oggetto della nota — tratto, targhetta, quota, immagine, forma —
  non solo i tratti), colore, misura, stato del salvataggio, ✕.
- **Redo** non esiste oggi: lo stack di azioni (`b2f8eb1`) va reso a due
  lati. Ogni azione ha già il suo inverso (è come undo funziona), quindi redo
  è «ri-applica l'azione tolta», e una nuova azione svuota il lato redo.
  Dopo il fix 20261008 OGNI passo, avanti o indietro, deve finire anche sul
  server (PUT della nota; DELETE se resta vuota).
- **Tastiera**: i tasti attuali restano (P, T, M, F, E…); `1/2/3` cambiano
  tab; Ctrl+Shift+Z / Ctrl+Y = redo.
- **Telefono** (<760 px): tab come segmenti in alto alla barra, riga strumento
  scorrevole in orizzontale, riga comune fissa. Verificare sul moto g05
  (`telefono_cattura`), non solo in headless.
- ✂ Sezione e 🔍 Aspetto **non** stanno sotto Disegna: servono a GUARDARE,
  anche senza disegnare nulla. Vanno nella barra della vista (accanto a
  Tutti / Inverti / Inquadra / Tag / Quote). *Da confermare con quill* (§7).

### Il registro degli strumenti — perché viene prima di tutto

`view.html` è un file solo di 4246 righe; ogni strumento oggi è sparso fra
HTML della barra, CSS `#vp[data-tool=…]`, switch dei tasti, `pointerdown` e
serializzazione della nota. Cinque subagenti che ci lavorano insieme si
pestano i piedi a ogni riga. Il passo 1 (§8, compito T0) introduce:

```js
registerTool({ id:'pen', tab:'pencil', key:'p', icon:'✎', title:'…',
               options: el => …,            // la riga opzioni (select stile, modi metro…)
               down/move/up(ev, hit),       // il gesto
               cursor:'crosshair' })
```

e sposta gli strumenti ESISTENTI dentro, a comportamento invariato. Da lì in
poi un compito nuovo aggiunge un `registerTool` in un suo modulo
(`webui/view-<x>.js`) invece di toccare cinque punti del monolite. Il
registro NON è un framework: è una tabella più un dispatcher, il resto resta
dov'è.

---

## 2. ✂ Sezione e 🔍 Aspetto

Si fanno come scritto in `PLAN_VIEW_SECTION.md` — fasi 1 e 2 di quel piano.
Qui solo i vincoli che vengono da questo:

- vivono in `viewer.js` / un modulo `webui/section.js`, così l'editor li
  eredita (fase 4 di quel piano, non adesso);
- il raycast (`firstHit`) deve ignorare il lato tagliato: tocca la penna, il
  piano di disegno (§4), il metro, le forme e le targhette — cioè tutti i tab.
  `firstHit` è UNO, è lì che va il filtro;
- la nota si porta il piano (`cut`) quando si disegna su una vista sezionata,
  o l'agente vede un buco che nel modello non c'è;
- §0 di quel piano resta il vincolo di prodotto: «Guarda dentro» mette
  l'involucro in vetro O in fantasma, mai un misto (vetro su vetro non si
  vede, fantasma dentro vetro neppure).

---

## 3. Targhe e tag che non coprono quello che indicano

Due codici quasi gemelli fanno oggi le targhette — quelle dell'utente
(view.html ~2961) e quelle dell'agente (~3998): `Sprite` con
`sizeAttenuation:true` disegnato due volte (pieno + fantasma 0,35 in
`depthTest:false`). Prima si unificano in un solo `makePlate()`, poi si
aggiunge il comportamento, una volta sola:

- **Dimensione a schermo, non in mm**: la targa ha un'altezza in PIXEL
  (default ~22 px, con un ⚙ «piccole / medie / grandi»), invece di mm che
  crescono con lo zoom. Avvicinandosi al dettaglio il pezzo si ingrandisce e
  la targa no: è esattamente il caso «cerco di vedere il problema».
- **Si scansa quando zoomi su ciò che indica**: se l'ancora è vicina al
  centro dello schermo e la camera vicina (distanza < k × misura della
  feature), la targa sfuma (opacità → ~0,15) e collassa nel solo
  pallino ◆ con lo stelo; torna piena allontanandosi. Isteresi, e un fade di
  ~150 ms, per non farla lampeggiare mentre si orbita.
- **Al passaggio del puntatore / al tocco** la targa collassata si riapre
  sopra a tutto: il testo resta leggibile quando lo cerchi.
- **Mai davanti al punto da cui parte**: lo stelo esce dalla parte della
  normale che si allontana dal centro dello schermo, così la targa non si
  piazza sulla linea di vista dell'ancora.
- **Opacità globale** nella barra della vista: oltre a ◆ on/off, uno slider
  (o tre stati: piene / traslucide / solo pallini). Va nell'hash (`tags=`
  diventa `tags=0|dot|ghost|1`, retrocompatibile).
- Vale per **etichette dell'agente, targhette dell'utente, quote del metro**
  (`b-measures`): sono lo stesso problema.

Verifica obbligatoria sul caso di quill, non su uno simile
(memoria `verifica-sul-caso-di-quill`): `creepyfinger-v4/g64#a1` (targhetta
da 30 mm sullo spigolo del dorso) e `g51` con i tag dell'agente accesi —
screenshot prima/dopo agli stessi zoom.

---

## 4. ✎ Matita — disegnare su un piano, anche nel vuoto

Oggi la penna disegna solo dove `surfaceHit` colpisce il pezzo; fuori dal
pezzo un trascinamento muove la vista. Quill vuole «disegni sui piani anche
se non c'è un pezzo», alla ZBrush: si fissa un piano di lavoro e la matita ci
scrive sopra.

- **⊞ Piano**, con cinque scelte: *Superficie* (il comportamento di oggi,
  default), *XY / XZ / YZ* (piani del mondo), *Vista* (perpendicolare alla
  camera). Il piano passa per l'**ultimo punto toccato sul pezzo** (Alt+clic
  sul pezzo per spostarlo lì), o per il centro dei pezzi visibili se non si è
  ancora toccato nulla.
- **Profondità alla ZBrush**: rotella con Shift (sul telefono, uno slider
  verticale a lato) sposta il piano lungo la sua normale, in passi di mm
  arrotondati come il pennello (`mmPerPx`). Il piano si vede: griglia leggera
  traslucida, 1 mm / 10 mm come la carta millimetrata delle ▣ Forme, e una
  linea dove taglia il pezzo (riusa il contorno CPU della ✂ sezione, §2).
- **Mescolare**: con un piano attivo, sopra il pezzo si disegna ancora SUL
  pezzo e fuori sul piano — un tratto che esce dal bordo continua sul piano
  invece di spezzarsi. Così «questo braccio va prolungato fin qui» si disegna
  con un gesto solo.
- **In conflitto con «fuori dal pezzo = muovi»**: col piano attivo il
  trascinamento a vuoto disegna. Restano il tasto destro, la rotella, due
  dita e lo strumento Muovi. Il suggerimento in barra lo dice.
- **Dati**: un tratto sul piano ha punti con `normal` = normale del piano e
  `on: null`, più `plane: {origin, normal}`. Il server (`marks`) li
  riconosce come `kind: "plane"`, li riassume come le altre gestualità e li
  aggancia al pezzo più vicino (`near_piece`, distanza in mm), perché per
  l'agente «un tratto a 6 mm sopra il dorso» è informazione.
- **Testo incollato sotto Matita**: T vernice e ▭ decal passano qui (sono
  inchiostro); la ⚑ targhetta va in Tag (§5). Il selettore di stile sparisce:
  sono tre strumenti, ognuno nel suo tab. La vernice funziona anche sul
  piano (stessa pipeline `surfaceHit` → piano).

---

## 5. ◆ Tag — e la nota diventa un tag, flottante sul progetto

Il tab Tag raccoglie ciò che PUNTA un posto e ci attacca qualcosa di
standard: ⚑ targhetta (testo), ↔ metro (quota), 🖼 Img (immagine), e il
nuovo 📝 Nota.

**Il campo nota sparisce dalla barra.** Al suo posto:

- **📝 Nota come tag**: un tocco sul pezzo (o sul piano) piazza una targhetta
  di tipo `note`, testo libero lungo, ancorata a quel punto. Esteticamente
  diversa (icona 📝, colore neutro), stesse regole di §3.
- **La nota flottante del progetto**: un pannello che si apre dalla barra
  della vista (e da sé quando ci sono note aperte), trascinabile, che elenca
  TUTTI i 📝 del progetto, di tutte le generazioni, con lo stato (aperta /
  risposta dell'agente), più un campo in cima per una nota senza ancora
  («per tutto il pezzo: stampalo in PETG»). Clic su una voce = apre il gen e
  inquadra il tag (`goToNote` esiste già).
- **Dati e retrocompatibilità**: la nota testuale di oggi (`text` della
  nota) resta nel formato: una nota senza ancora la scrive lì, e un 📝 è un
  label con `style: "note"`. Così `cad_notes` non cambia forma, e le note
  vecchie si vedono nel pannello con la loro `text` come voce senza ancora.
  Un 📝 di progetto senza gen (scritto dal pannello mentre nessun gen è
  aperto) ha bisogno di un posto: `projects/<name>/notes/` accanto a
  `gens/`, o la nota sull'ULTIMO gen. *Da decidere* (§7) — cambia
  backend, `cad_notes` e AGENT_HELP.

---

## 6. ▣ Blocky

Le ▣ Forme di oggi (cubo/cilindro/sfera, Sposta/Ruota/Gabbia Scala|Deforma,
misure standard) passano tutte in questo tab, senza cambiare comportamento.
I modi del gizmo (✥ ⟳ ▣ ◌), oggi in una barra che compare sopra la forma
selezionata (`#s-bar`), diventano la riga strumento del tab — una barra in
meno che galleggia sul pezzo.

Quello che il nome promette in più («li trasformi e **modelli**») e che non
c'è: duplicare una forma (Ctrl+D), unire/sottrarre forme fra loro per dire
«un foro così» (anteprima mesh con manifold-wasm, già vendorizzato per
`anticipate.js`), e una forma «estrusa» da un tratto chiuso della matita.
**Non in questo giro**: lo segno come fase 3, da far confermare.

---

## 7. Da decidere con quill prima del codice

1. **✂ e 🔍 fuori da Disegna**, nella barra della vista? (proposta: sì.)
2. **La nota flottante**: un posto per progetto (`projects/<n>/notes/`) o
   sempre attaccata a un gen? (proposta: per progetto, il gen diventa un
   campo facoltativo — è quello che «di tutto il progetto» dice.)
3. **Targhe a pixel fissi** al posto dei mm: cambia l'aspetto di TUTTE le
   note salvate. (proposta: sì, `size_mm` resta nei dati per l'agente.)
4. **Blocky «modella»** (booleane fra forme, estrusione di un tratto): ora o
   dopo?
5. **PR**: la pila sopra la #28 è a 5 branch. Si continua a impilare o si
   chiede di mergiare la #28 prima?

---

## 8. Subagenti — chi fa cosa, in che ordine

Ogni compito in una **worktree sua** (`git worktree add ../noodle-vt-<x> -b
feat/vt-<x> <base>`), MAI cambiando branch in `~/projects/noodle` (condiviso
con altri agenti). Prove su container usa e getta che monta la worktree
(modello `noodle-measure`: 127.0.0.1:8091+, progetti COPIATI nello
scratchpad), mai sul quadlet `noodle`. Test con
`nix develop ~/nixos/shells#noodle -c python -m pytest tests` e
`node tests/ui/*.test.cjs`. Ogni agente consegna: commit sul suo branch,
screenshot prima/dopo, cosa NON ha verificato.

```
fase 1 (serie)      T0 registro + tab + riga comune + redo
                         │
fase 2 (parallelo)  ├─ A ✂ Sezione        (viewer.js + section.js)
                    ├─ B 🔍 Aspetto/fantasma (viewer.js makeMaterial + lista pezzi)
                    ├─ C targhe che si scansano (view-plates.js)
                    ├─ D ⊞ piano della matita (view-plane.js)
                    └─ E 📝 nota-tag + pannello progetto (view-notes.js + server/api)
                         │
fase 3 (serie)      I  integrazione, prove sul caso di quill, docs, deploy
```

| id | compito | file | dipende | conflitti attesi |
|----|---------|------|---------|------------------|
| **T0** | registro strumenti; tab Matita/Tag/Blocky; riga comune; redo (anche sul server); campo nota rimosso dalla barra (il testo resta raggiungibile finché E non arriva: un 📝 provvisorio che apre il vecchio campo) | view.html | — | tutti: per questo è da solo e prima |
| **A** | `PLAN_VIEW_SECTION` fase 1 + filtro in `firstHit` + `cut` nella nota e nell'hash | viewer.js, webui/section.js, view.html (bottone, hash) | T0 | B su viewer.js (zone diverse: A `renderPreviews`, B `makeMaterial`) |
| **B** | `PLAN_VIEW_SECTION` fase 2: menu per foglia, `look=`, finitura fantasma (anche editor), «Guarda dentro»; prova su telefono vero | viewer.js, view.html (righe lista) | T0 | A |
| **C** | §3: `makePlate` unico, px fissi, sfuma/collassa vicino all'ancora, hover riapre, `tags=` a 4 stati, anche quote | webui/view-plates.js, view.html (2 punti di creazione) | T0 | E (anche il 📝 è una targa: E usa `makePlate` di C → C consegna prima l'API, anche come stub) |
| **D** | §4: piano di lavoro, profondità, griglia, tratti misti, `kind:"plane"` lato server, vernice sul piano | webui/view-plane.js, view.html (`surfaceHit`), cad_nodes notes (marks) | T0 (+ contorno di A, facoltativo: senza, niente linea di taglio sulla griglia) | E lato server (`marks`) |
| **E** | §5: 📝 come label `style:"note"`, pannello flottante del progetto, posto per le note di progetto (secondo §7.2), `cad_notes`/AGENT_HELP | webui/view-notes.js, server.py, cad_nodes/api.py + notes, mcp_server.py | T0, API di C | D (server) |
| **I** | merge A–E in ordine (A, B, C, D, E), test completi, screenshot su `creepyfinger-v4` g51/g64 e `bolt-and-nut`, CLAUDE.md §9c, AGENT_HELP (`cut=`, `look=`, `tags=`, note di progetto), deploy su noodle-dev e — solo dopo l'ok di quill — sul gpunix | tutti | A–E | — |

Regole per chi dirige (io, nella sessione principale):

- **T0 non si parallelizza** e si guarda prima di lanciare la fase 2: se il
  registro non regge, cinque agenti costruiscono sul vuoto.
- Ogni prompt di subagente contiene: il paragrafo di questo piano, la base
  esatta (commit), la worktree, la porta del suo container, i file che PUÒ
  toccare e quelli che NON deve toccare, e «riproduci sul caso di quill».
- Nessun agente pusha, apre PR o tocca il gpunix: lo fa I, dopo quill.
- Un agente che trova il piano sbagliato si ferma e lo dice, non improvvisa
  un'altra interfaccia.
