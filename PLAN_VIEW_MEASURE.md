# 📏 Metro — misurare nella preview, e far quotare all'agente

> Piano per una sessione nuova (scritto il 2026-10-08, a fine di quella che ha
> fatto ✎ Disegna). Non è implementato niente: qui ci sono il problema, il
> progetto proposto, le decisioni ancora da prendere e dove mettere le mani.

## Il problema

Nel viewer `/view/<graph>/gN` l'utente può già disegnare sul pezzo, scriverci e
mandare la nota all'agente (✎ Disegna, CLAUDE.md §9c), e l'agente può
etichettare i pezzi (`cad_tag_gen`). Manca la cosa che in un CAD si fa più spesso
di tutte: **misurare**.

- **L'utente** vuole verificare: «quanto è spessa questa parete?», «quel foro è
  davvero 8?», «quanto c'è fra questi due fori?». Oggi deve chiederlo all'agente
  o aprire l'editor.
- **L'agente** vuole indicare: «questa parete è 1,2 mm, sotto il minimo di 1,6
  per la stampa», «interasse 30 ±0,1: controllalo». Oggi può solo scriverlo nella
  risposta, staccato dal pezzo.

Lo stesso oggetto serve a tutti e due: una **quota** disegnata sul pezzo.

## Il progetto in breve

Una terza specie di targhetta, accanto a quelle di testo e ai tag dell'agente,
con la **stessa grafica**: ancora + gambo + targa sempre rivolta alla camera,
visibile attenuata quando il pezzo la copre (vedi `tagObject()` in
`webui/view.html`). Invece di un'ancora sola ne ha **due**, unite da una linea
di quota con le frecce/tacche alle estremità, e la targa sta a metà con il
valore: `12,40 mm`, `Ø 8,00`, `R 4,00`, `90,0°`.

- **Strumento 📏 Metro** nella barra di ✎ Disegna, accanto a T / Img / gomma.
- **Le quote dell'utente** vanno nella nota come dato (`measures`), come i
  `labels`: l'agente le legge con `cad_notes` («misura: 1,21 mm fra la faccia
  interna e quella esterna del bicchiere; l'utente ha scritto "troppo sottile"»).
- **Le quote dell'agente** stanno accanto alla generazione (come `tags.json`),
  con uno **stato** che decide il colore: `ok` verde, `check` ambra, `fail`
  rosso. Così una generazione arriva all'utente già annotata con quello che va
  guardato.
- Funziona tutto anche nell'anteprima statica su GitHub Pages: misurare è
  calcolo nel browser.

## Il punto difficile: dove si attacca il metro

Il viewer **non ha la topologia B-Rep**: `view.json` contiene solo triangoli.
Facce, spigoli, vertici e cerchi vanno quindi **ricostruiti dalla mesh**, nel
browser. Si può fare bene, perché le tassellature build123d hanno spigoli vivi
netti e facce piane esatte.

### Menu all'attivazione: «Cosa vuoi misurare?»

Quando l'utente sceglie 📏 compare un piccolo menu (segmented control, come gli
stili di T), ricordato in localStorage:

| Modo | Clic | Cosa misura | Come si trova sulla mesh |
|---|---|---|---|
| **Auto** (default) | 2 | la cosa giusta per ciò che tocchi | inferenza a priorità (sotto) |
| **Punto–punto** | 2 | distanza fra due punti della superficie | raycast, come la penna |
| **Spigolo** | 1 | lunghezza di uno spigolo dritto | spigoli vivi (sotto), il segmento colinearizzato |
| **Foro / cerchio** | 1 | Ø e R di un bordo circolare o di una faccia cilindrica | fit di cerchio sugli spigoli vivi chiusi, o il fit di cilindro dalle normali che c'è già (`fitSurface`) |
| **Faccia–faccia** | 2 | spessore / distanza fra due facce piane parallele | patch planare per flood-fill di normali, poi distanza piano-piano |
| **Angolo** (v2) | 2 | angolo fra due spigoli o due facce | direzioni / normali delle due feature |

**Auto è quella che conta**, ed è come fanno i CAD veri: mentre il puntatore si
muove, sotto il dito si evidenzia la feature che verrebbe presa, con un'icona
(◆ vertice, — spigolo, ○ cerchio, ▭ faccia). La priorità va da quella più
specifica alla più generica:

1. **vertice vivo** entro ~10 px (dito ~18 px);
2. **centro di un cerchio** se il puntatore è vicino a un bordo circolare
   (prende il centro, non il bordo: è ciò che si vuole quando si misura un
   interasse);
3. **spigolo vivo** entro ~8 px (punto più vicino sullo spigolo);
4. **faccia piana** (ricorda il piano per il caso faccia–faccia);
5. punto libero sulla superficie.

**Le combinazioni di Auto:**
- vertice→vertice = distanza;
- centro→centro = interasse;
- faccia∥faccia = spessore, perpendicolare;
- punto→faccia = distanza dal piano;
- spigolo da solo (doppio clic o «Fine») = lunghezza;
- cerchio da solo = Ø.

Con il mouse **Shift** blocca la quota su X/Y/Z, come i CAD. Sul telefono una
**lente** sopra il dito mostra cosa sta per essere preso, perché il dito copre
il punto.

### Ricostruire le feature dalla mesh

- **Spigoli vivi:** `THREE.EdgesGeometry(geometry, 25–30°)` sul mesh del pezzo,
  calcolata **pigramente** al primo hover su quel pezzo e messa in cache per
  oggetto. Un indice spaziale semplice (griglia sui segmenti) evita un loop su
  tutti i segmenti a ogni mossa. Attenzione alle superfici a creste (filetti):
  migliaia di spigoli. Lì conviene un limite o un avviso «snap disattivato su
  superfici fitte», e resta punto–punto.
- **Vertici vivi:** gli estremi dei segmenti vivi dove si incontrano ≥ 3 spigoli,
  o dove la direzione cambia più di ~20°.
- **Cerchi:** catene chiuse di spigoli vivi, cioè loop. Fit del piano (PCA) e poi
  fit di cerchio 2D (Kåsa, o algebrico + un paio di passi di Gauss-Newton).
  Accetta se il residuo è < 1–2% del raggio. Per un foro visto «dentro», il fit
  di cilindro dalle normali (`fitSurface`, già nel codice del decal, commit
  `0a37138`) dà asse e raggio.
- **Facce piane:** flood-fill dai triangoli adiacenti con normale entro ~1° da
  quella colpita. Ne vengono il piano (punto + normale) e il contorno per
  l'evidenziazione.

### Precisione: dirlo, non nasconderlo

- Le facce piane e i vertici vivi della tassellatura sono **esatti**: build123d
  mette i vertici sulla geometria vera.
- I cerchi tassellati sono poligoni inscritti: il raggio fittato è affidabile
  quasi al centesimo con le tassellature normali, ma **è un'approssimazione**.
  La targa mostra `≈` quando il valore viene da una feature curva tassellata.
- Il valore **esatto** lo dà il B-Rep, cioè il server (vedi fase 5). Nella
  preview statica resta `≈`.

## I dati

### Quote dell'utente, nella nota
Accanto a `strokes`, `labels` e `images` (`api.add_note`, validazione come per
i labels):

```jsonc
"measures": [{
  "kind": "distance" | "edge" | "diameter" | "radius" | "face_gap" | "angle",
  "a": {"at": [x,y,z], "snap": "vertex|circle_center|edge|face|free", "node": "n8",
        "piece": "n8.0", "normal": [..], "circle": {"center": [..], "axis": [..], "r": 4.0}},
  "b": { ... },                          // assente per edge/diameter/radius
  "value": 12.4, "unit": "mm" | "deg", "approx": true,
  "text": "deve essere 12",              // opzionale: l'utente ci scrive sopra
  "view": 2                              // la foto della vista in cui è stata presa
}]
```

`cad_notes` le espone con `near_marks`, come i labels, e le cita nel testo
riassuntivo per i modelli solo-testo.

### Quote dell'agente, accanto alla generazione
`gens/gN/measures.json`, scritto da `api.measure_gen(store, graph, gen, measures,
replace=True)`. HTTP `POST|GET /api/graph/{name}/gens/{gen}/measures`, MCP
`cad_measure_gen`, e `measures=[…]` opzionale in `cad_snapshot`, come `tags=`.

```jsonc
{"text": "parete 1,2 mm", "a": {"at": [..]}, "b": {"at": [..]}, "kind": "face_gap",
 "value": 1.2, "status": "fail", "expected": 1.6, "tolerance": 0.1, "note": "sotto il minimo per FDM"}
```

**L'agente non deve indovinare i punti.** `cad_measure` sa già la distanza fra
due nodi e i due punti più vicini (`measure.py::distance` → `at_a`, `at_b`).
Basta documentare in AGENT_HELP il giro «misura con `cad_measure`, poi posa la
quota con quei punti». Valuta un `measure_gen(..., from_measure={a, b})` che lo
fa in un colpo solo.

Nel viewer: colore per `status`, la targa mostra `valore` e, se c'è,
`atteso ± tolleranza`. Un interruttore «📏 Quote» nell'hash (`#measures=0`),
come «◆ Tag». Tocco su una quota → dettaglio (nota dell'agente, atteso, stato).

## Fasi proposte (una PR per fase o due)

1. **La quota come oggetto grafico** — FATTA (`measureObject` in view.html,
   `window.__noodleMeasures.draw(list)` per i dati finti, bottone «↔ Quote» +
   `#measures=0`)
   - `measureObject(a, b, value, style)` in view.html, accanto a `tagObject`: due
     ancore, linea con tacche, targa a metà, la stessa doppia copia
     depthTest/attenuata.
   - Disegno da dati finti; verifica davanti/dietro/telefono.
2. **Punto–punto + menu**
   - Lo strumento 📏, il menu dei modi (solo Punto–punto attivo), quota nella
     bozza, gomma e undo (la pila di azioni c'è già), foto della vista.
3. **Snap**
   - Spigoli vivi, vertici, facce piane, cerchi; evidenziazione al passaggio;
     modo Auto con priorità; Spigolo / Foro / Faccia–faccia; lente sul telefono.
   - Test pure-JS delle funzioni di fit (estrarle come `anticipate.js`, testate in
     `tests/ui/*.test.cjs`).
4. **Dati**
   - `measures` nella nota (backend + `cad_notes`); `measures.json` dell'agente
     (store / api / server / MCP / `cad_snapshot`), stato e colori.
   - AGENT_HELP: il giro «cad_measure → quota».
   - Anteprima statica: `build_pages.py` pubblica `measures.json` come `tags.json`.
5. **Esatto dal server** (facoltativo)
   - Nel `/view` servito da noodle, un «verifica esatto» che manda le due feature
     al server.
   - Il server esegue il grafo congelato della generazione e misura sul B-Rep vero
     (`measure.py`, `executor.measure_graph`), poi sostituisce `≈` con il valore
     esatto.
   - Costa un'esecuzione: va bene su richiesta, non a ogni misura.

## Decisioni prese (2026-10-08, con quill)

- **Unità:** mm, 2 decimali, virgola decimale (`12,40 mm`, `Ø 8,00`); angoli 1
  decimale. Nessun interruttore in/mm.
- **Angolo:** dopo la fase 4, come v2.
- **Quote dell'agente:** file a parte, `gens/gN/measures.json`.
- **Pezzi animati:** coordinate mondo + `t`, come le note di ✎ Disegna.
- **Snap/lente (da tarare in fase 3):** 10 px mouse / 18 px dito; lente ~90 px,
  2×, ~110 px sopra il dito.
- **Superfici fitte:** oltre ~20k segmenti di spigolo vivo su un pezzo lo snap si
  spegne su quel pezzo (resta punto–punto), con avviso.

## Da decidere prima di scrivere codice (testo originale)

- **Unità e decimali:** mm con 2 decimali, virgola decimale (UI italiana)? Un
  interruttore in/mm serve?
- **Angolo:** fase 3 o rimandato? Serve alle pendenze per la stampa (overhang),
  ma è un altro tipo di quota.
- **Quote dell'agente in `tags.json` o in un file a parte?** Un file a parte
  tiene semplici le due validazioni e gli interruttori separati. Consigliato.
- **Raggio di snap e lente sul telefono:** quanto offset sopra il dito.
- **Filetti e superfici fitte:** soglia oltre la quale lo snap si spegne sul
  pezzo (numero di spigoli vivi).
- **Quote sui pezzi animati:** la quota segue il pezzo (coordinate locali del
  pezzo) o resta nello spazio mondo al `t` corrente? Per le note di ✎ Disegna si
  salva `t`: probabilmente basta lo stesso.

## Dove mettere le mani (riferimenti)

- `webui/view.html`, il blocco «✎ Disegna»:
  - `firstHit` / `surfaceHit` (raycast);
  - `mmPerPx`;
  - `tagObject` (la grafica della targhetta);
  - `fitSurface` (fit di cilindro/sfera, riusabile per i fori);
  - la pila di azioni dell'undo;
  - `takeView` / foto delle viste;
  - `sendNote`;
  - `loadNotes` / `renderNotes`;
  - i tag dell'agente (`tags.json`, interruttore «◆ Tag»).
- `cad_nodes/api.py`: `add_note` (validazione labels da imitare), `_marks`,
  `_note_for_agent`, `tag_gen`.
- `cad_nodes/store.py`: i file accanto alla gen.
- `server.py` e `mcp_server.py`: le route e i tool dei tag, da imitare.
- `cad_nodes/measure.py`: `distance` restituisce `at_a`/`at_b`.
- `cad_nodes/AGENT_HELP.md`: la parte su `cad_notes` / `cad_tag_gen`.
- `scripts/build_pages.py`: shim delle fetch dell'anteprima statica.
- `CLAUDE.md` §9c: va aggiornato a fine lavoro.

## Come si lavora qui (lezioni della sessione precedente)

- **Il working tree `~/projects/noodle` è condiviso con altri agenti.** Lavora
  in una worktree a parte, partendo da `origin/main`; niente cambi di branch là
  dentro, `git add` solo dei tuoi file.
- **Prova sul vero, nel browser:** Playwright gira nel container `noodle`
  (`docker cp` script → `docker exec noodle python`), avvio di Chromium con
  `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader`. Guarda
  gli screenshot, non fidarti dei numeri. Il touch si simula con
  `Input.dispatchTouchEvent` via CDP.
- **Progetto di prova:** `zz-note-probe` (bullone e dado, gen g1/g2). Copie usa
  e getta per le modifiche; non scrivere sui progetti dell'utente.
- **Il server di sviluppo** è il quadlet utente `noodle` sul mini PC, con il
  codice montato da `~/projects/noodle`. Dopo modifiche backend:
  `systemctl --user restart noodle`. Dalla LAN: `noodle-dev.svc.lan` / `:8190`.
- **Prima di pushare su una PR controlla che non sia già stata mergiata**
  (`gh pr view N --json state`). Le PR vanno da `quill4gen7:<branch>` verso
  `rederyk/noodle:main`.
- **Produzione = gpunix** (`~/services/noodle`, `main`): `git pull --ff-only`, e
  restart solo se cambia il backend.
- **Anteprima statica:** `scripts/build_pages.py --base-site <checkout
  gh-pages>`, perché le demo vecchie esistono solo lì. Si pubblica sul ramo
  `gh-pages` del fork.
