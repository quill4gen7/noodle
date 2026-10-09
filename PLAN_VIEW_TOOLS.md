# /view — strumenti in tab, vedere dentro, targhe che non coprono

Piano. Richiesta di quill (2026-10-09), decisioni prese con lui lo stesso
giorno (§7). Raccoglie anche `PLAN_VIEW_SECTION.md` (✂ Sezione + 🔍 Aspetto,
pianificati l'8/10): lì il COME è già studiato e misurato, qui si decide il
DOVE e l'ordine.

Base: `plan/view-section` (3c527d4) = tutta la pila non mergiata
`feat/view-measure` (PR #28) → `feat/view-shapes` → `feat/view-pen3d` →
`fix/feedback-20261008-153010-creepyfinger-v4` + il piano della sezione.
**Si impila sopra; il rebase su main si fa dopo, tutto insieme.**

---

## 0. Cosa chiede quill, e cosa dicono già i dati

1. **colore e soprattutto materiale ai pezzi** — trasparente o luminoso, per
   vedere meglio (= `PLAN_VIEW_SECTION` §2, 🔍 Aspetto);
2. **✂ sezione** con la classica campitura a righe (= `PLAN_VIEW_SECTION` §1);
3. **targhe e tag troppo davanti** quando cerchi di guardare il punto che
   indicano;
4. **✎ Disegna diventa una barra a tab** con una riga comune (↶ ↷ ⌫);
5. **niente più campo nota**: la nota è un tag, e non è obbligatoria;
6. (implicito) tutto resta salvato da solo, come dal fix del 20261008.

Cosa dicono le note vere (`cad_notes`, creepyfinger-v4, notte del 9/10):

- g49#a1, g49#a2, g51#a1, g51#a2 sono disegnate con `tags=0` nell'hash:
  quill spegne TUTTE le etichette dell'agente per vedere dove disegnare.
- g51#a2 è fatta solo di targhette (testo nota vuoto); g57#a2, g58#a1,
  g63#a1, g63#a3 hanno il testo vuoto. Quill scrive DOVE, non nel campo.
- Le note aperte (g62#a1, g58#a1/a2, g57#a2, g63#a3, g47#a4) riguardano il
  MODELLO: fuori da questo piano.

---

## 1. La nuova barra

```
┌ [✎ Matita] [◆ Tag] [▣ Blocky] [🔧 Tool] ────── ↶ ↷ ⌫ │ colori │ misure │ stato ✕ ┐
│  riga dello strumento (cambia col tab):                                          │
│  Matita: ✎ penna · ✎³ penna 3D · T vernice · ▭ decal · ⊞ piano [Sup|XY|XZ|YZ|Vista] │
│  Tag:    ⚑ targhetta · 🖼 img                                                     │
│  Blocky: ▣ cubo/cilindro/sfera · ✥ ⟳ ▣ gabbia (Scala|Deforma) ◌                   │
│  Tool:   ↔ metro [modo] · ✂ sezione [X|Y|Z, posizione, ⇄, per pezzo]              │
└──────────────────────────────────────────────────────────────────────────────────┘
```

- **Riga comune**, identica in tutti i tab: ↶ annulla, ↷ ripeti, ⌫ gomma
  (cancella QUALUNQUE oggetto della nota — tratto, targhetta, quota,
  immagine, forma — non solo i tratti), colore, misura, stato del
  salvataggio, ✕.
- **↷ Redo** non esiste oggi: lo stack di azioni (`b2f8eb1`) diventa a due
  lati; ogni azione ha già il suo inverso, redo la ri-applica, una nuova
  azione svuota il lato redo. Dopo il fix 20261008 OGNI passo, avanti o
  indietro, finisce anche sul server (PUT della nota; DELETE se resta vuota).
- **Il campo nota sparisce.** La nota è fatta di quello che si disegna e si
  scrive SUL pezzo (⚑ targhetta, T vernice). Nessun campo obbligatorio,
  nessun pannello di progetto. Il formato non cambia: `text` resta nei dati
  (vuoto per le note nuove; le vecchie lo mostrano ancora in lettura) e
  `cad_notes` non cambia forma.
- **Tastiera**: i tasti attuali restano (P, T, M, F, E…); `1/2/3/4` cambiano
  tab; Ctrl+Shift+Z / Ctrl+Y = redo.
- **Telefono** (<760 px): tab come segmenti in alto alla barra, riga
  strumento scorrevole in orizzontale, riga comune fissa. Verificare sul moto
  g05 (`telefono_cattura`), non solo in headless.
- **🔍 Aspetto** (vetro / emissivo / fantasma / colore per pezzo) non è uno
  strumento di disegno: sta nella lista dei pezzi (menu per riga) e nella
  barra della vista, come da `PLAN_VIEW_SECTION` §2.
- **✂ Sezione è anche fuori da Disegna**: nel tab 🔧 Tool per chi sta
  annotando, e con lo stesso stato raggiungibile dalla barra della vista
  (✂ accanto a Tutti / Inverti / Inquadra) per chi vuole solo guardare. UNO
  stato, due bottoni: non due sezioni.

### Il registro degli strumenti — perché viene prima di tutto

`view.html` è un file solo di 4246 righe; ogni strumento è sparso fra HTML
della barra, CSS `#vp[data-tool=…]`, switch dei tasti, `pointerdown` e
serializzazione della nota. Cinque subagenti insieme si pestano i piedi a
ogni riga. Il compito T0 (§8) introduce:

```js
registerTool({ id:'pen', tab:'pencil', key:'p', icon:'✎', title:'…',
               options: el => …,            // la riga opzioni (stile, modi metro…)
               down/move/up(ev, hit),       // il gesto
               cursor:'crosshair' })
```

e sposta gli strumenti ESISTENTI dentro, a comportamento invariato. Da lì
un compito nuovo aggiunge un `registerTool` in un suo modulo
(`webui/view-<x>.js`) invece di toccare cinque punti del monolite. Non è un
framework: una tabella più un dispatcher, il resto resta dov'è.

---

## 2. ✂ Sezione e 🔍 Aspetto

Come scritto in `PLAN_VIEW_SECTION.md`, fasi 1 e 2. Vincoli che vengono da qui:

- vivono in `viewer.js` / un modulo `webui/section.js`, così l'editor li
  eredita più avanti;
- il raycast (`firstHit`) deve ignorare il lato tagliato: tocca penna, piano
  di disegno (§4), metro, forme, targhette. `firstHit` è UNO, il filtro va lì;
- la nota si porta il piano (`cut`) quando si disegna su una vista
  sezionata, o l'agente vede un buco che nel modello non c'è;
- «Guarda dentro» mette l'involucro in vetro O in fantasma, mai un misto
  (vetro su vetro non si vede, fantasma dentro vetro neppure).

---

## 3. Targhe che si scansano quando ci zoommi dentro

Le targhe restano come sono — misura in mm (`sizeAttenuation`), piene,
lette da ogni lato — **finché si legge la parola intera**. Il problema nasce
solo quando zoommi così vicino che della targa vedi le singole LETTERE e non
più la parola: a quel punto la targa è più grande di quello che indica e lo
copre.

- **Quando**: la targa proiettata sullo schermo non è più leggibile per
  intero — larghezza proiettata oltre ~60% del viewport, o il rettangolo del
  testo esce dallo schermo, o l'altezza delle lettere supera ~3× quella di
  lettura (tarare a occhio su g64#a1). Con isteresi (entra a 1,0, esce a
  0,8) per non farla lampeggiare mentre si orbita o si gira la rotella.
- **Cosa fa**: si **allontana dal punto dove stai zoommando** (la posizione
  del cursore/delle dita della rotella o del pinch, altrimenti il centro
  dello schermo) — scivola lungo lo stelo, verso fuori, finché esce dalla
  zona di zoom — **e/o diventa trasparente** (opacità ~0,15). Ordine: prima
  scivola; se non c'è spazio (scivolando uscirebbe dallo schermo), sfuma.
  Transizione ~150 ms. L'ancora (pallino) resta dov'è: indica ancora il
  posto.
- **Torna** com'era quando ti allontani abbastanza da rileggere la parola.
- **Niente pixel fissi, niente stati in più nella barra**: ◆ on/off resta
  com'è.
- Vale per **targhette dell'utente, etichette dell'agente e quote del metro**.
  Oggi due codici quasi gemelli fanno le targhette (view.html ~2961 utente,
  ~3998 agente): prima si unificano in un `makePlate()`, poi il
  comportamento si scrive una volta.

Verifica obbligatoria sul caso di quill (memoria `verifica-sul-caso-di-quill`):
`creepyfinger-v4/g64#a1` (targhetta da 30 mm sullo spigolo del dorso) e
`g51` con i tag dell'agente accesi — screenshot a tre zoom: lontano (targa
piena), medio (parola ancora intera, targa ferma), vicino (scansata).

---

## 4. ✎ Matita — disegnare su un piano, anche nel vuoto

Oggi la penna disegna solo dove `surfaceHit` colpisce il pezzo; fuori dal
pezzo un trascinamento muove la vista. Quill vuole disegnare «sui piani
anche se non c'è un pezzo», alla ZBrush.

- **⊞ Piano**: *Superficie* (oggi, default), *XY / XZ / YZ*, *Vista*
  (perpendicolare alla camera). Passa per l'ultimo punto toccato sul pezzo
  (Alt+clic sul pezzo per spostarlo lì), o per il centro dei pezzi visibili.
- **Profondità**: Shift+rotella (telefono: slider verticale a lato) sposta il
  piano lungo la normale, a passi di mm arrotondati come il pennello. Il
  piano si vede: griglia traslucida 1/10 mm come la carta millimetrata delle
  ▣ Forme, più la linea dove taglia il pezzo (riusa il contorno CPU della ✂,
  se A è già dentro; senza, niente linea).
- **Misto**: col piano attivo, sopra il pezzo si disegna SUL pezzo e fuori
  sul piano; un tratto che esce dal bordo continua sul piano invece di
  spezzarsi.
- **Conflitto con «fuori dal pezzo = muovi»**: col piano attivo il
  trascinamento a vuoto disegna. Restano tasto destro, rotella, due dita,
  Muovi. Il suggerimento in barra lo dice.
- **Dati**: punti con `normal` = normale del piano, `on: null`,
  `plane: {origin, normal}`. Lato server (`marks`) `kind: "plane"` e
  `near_piece` (pezzo più vicino + distanza mm).
- **Testo incollato sotto Matita**: T vernice e ▭ decal sono inchiostro e
  stanno qui; la ⚑ targhetta va in Tag. Il selettore di stile sparisce: sono
  strumenti separati. La vernice funziona anche sul piano.

---

## 5. ◆ Tag

⚑ targhetta (testo) e 🖼 Img: ciò che PUNTA un posto e ci attacca qualcosa.
Comportamento invariato, cambiano solo posto e il §3. Il metro NON è qui: è
in 🔧 Tool con la sezione.

## 6. ▣ Blocky

Le ▣ Forme di oggi, **così come sono**: posare cubo/cilindro/sfera,
Sposta / Ruota / Gabbia Scala|Deforma (FFD), misure standard. Nessuna
booleana, nessuna estrusione, niente «modella». L'unico cambiamento: i modi
del gizmo (✥ ⟳ ▣ ◌), oggi nella barra che galleggia sopra la forma
(`#s-bar`), diventano la riga strumento del tab; dimensioni e preset
(1mm/10mm/½ vol, Togli) restano vicini alla forma selezionata.

---

## 7. Decisioni (quill, 2026-10-09)

1. ↔ Metro e ✂ Sezione in un tab a parte, **🔧 Tool**, fuori da Tag.
2. **Nota di progetto: deprecata.** Meglio un tag; la nota non è
   obbligatoria. Niente pannello flottante, niente `projects/<n>/notes/`.
3. **Targhe**: restano in mm; solo quando sei così vicino da vedere le
   lettere e non più la parola intera, si allontanano da dove zoommi e/o
   diventano trasparenti.
4. **Blocky** = sposta/ruota/deforma come oggi. Niente booleane né estrusioni.
5. **Branch**: si impila sulla pila esistente; rebase su main dopo.

---

## 8. Subagenti — chi fa cosa, in che ordine

Ogni compito in una **worktree sua**, impilata: `git worktree add
../noodle-vt-<x> -b feat/vt-<x> <base>`. MAI cambiare branch in
`~/projects/noodle` (condiviso con altri agenti). Prove su container usa e
getta che monta la worktree (modello `noodle-measure`: 127.0.0.1:8091+,
progetti COPIATI nello scratchpad), mai sul quadlet `noodle`. Test:
`nix develop ~/nixos/shells#noodle -c python -m pytest tests` e
`node tests/ui/*.test.cjs`. Ogni agente consegna: commit sul suo branch,
screenshot prima/dopo, cosa NON ha verificato.

```
fase 1 (serie)      T0 registro + 4 tab + riga comune + redo + via il campo nota
                         │  (base: plan/view-tools)
fase 2 (parallelo)  ├─ A ✂ Sezione (in 🔧 Tool + barra vista)
   tutti da T0      ├─ B 🔍 Aspetto / fantasma (lista pezzi)
                    ├─ C targhe che si scansano
                    └─ D ⊞ piano della matita
                         │
fase 3 (serie)      I  integrazione (merge A→B→C→D), prove, docs, deploy
```

| id | compito | file | conflitti attesi |
|----|---------|------|------------------|
| **T0** | registro strumenti; tab Matita/Tag/Blocky/Tool con gli strumenti di oggi spostati a comportamento invariato (metro in Tool; stile T diviso in vernice/decal in Matita e targhetta in Tag); riga comune; redo (anche sul server); campo nota tolto; segnaposto ✂ in Tool per A | view.html | tutti: per questo è da solo e prima |
| **A** | `PLAN_VIEW_SECTION` fase 1 + filtro in `firstHit` + `cut` nella nota e nell'hash; bottone in Tool e nella barra vista | viewer.js, webui/section.js, view.html | B su viewer.js (A `renderPreviews`, B `makeMaterial`) |
| **B** | `PLAN_VIEW_SECTION` fase 2: menu per foglia, `look=`, finitura fantasma (anche editor), «Guarda dentro»; prova su telefono vero | viewer.js, view.html (righe lista) | A |
| **C** | §3: `makePlate` unico, scansamento/trasparenza quando la parola non si legge più, anche quote | webui/view-plates.js, view.html (2 punti di creazione) | — |
| **D** | §4: piano di lavoro, profondità, griglia, tratti misti, `kind:"plane"` lato server, vernice sul piano | webui/view-plane.js, view.html (`surfaceHit`), notes lato server | — |
| **I** | merge, test completi, screenshot su `creepyfinger-v4` g51/g64 e `bolt-and-nut`, CLAUDE.md §9c, AGENT_HELP (`cut=`, `look=`, campo nota non più usato), deploy su noodle-dev; gpunix solo dopo l'ok di quill | tutti | — |

Regole per chi dirige (la sessione principale):

- **T0 non si parallelizza** e si guarda prima di lanciare la fase 2.
- Ogni prompt di subagente contiene: il paragrafo di questo piano, la base
  esatta (commit), la worktree, la porta del suo container, i file che PUÒ
  toccare e quelli che NON deve toccare, e «riproduci sul caso di quill».
- Nessun agente pusha, apre PR o tocca il gpunix.
- Un agente che trova il piano sbagliato si ferma e lo dice, non improvvisa.
