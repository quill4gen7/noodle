# ✂ Sezione e 🔍 Aspetto in /view — vedere DENTRO il pezzo

Piano, non ancora codice. Richiesta di quill (2026-10-08): «mettere un altro
strumento sezione per tagliare verso un piano i pezzi e vederli dentro con la
classica texture a righe oblique per capire il taglio; cambiare colore ai pezzi
per usare il vetro e l'emissivo così da vedere pezzi dentro altri — da provare
se il vetro fa trasparire solo le luci o anche i pezzi normali colorati o altri
vetri».

Due strumenti, un solo scopo: oggi in /view un pezzo dentro un altro (la vite
nel dado, il servo nella scocca) si vede solo NASCONDENDO il pezzo che lo
copre — e si perde il rapporto fra i due, che era proprio quello da guardare.

---

## 0. Cosa fa il vetro OGGI — misurato, non supposto

Scena di prova `zz-glass-probe` (poi cancellata): un box di vetro con dentro
tre sfere — una normale rossa, una emissiva gialla, una di vetro blu — e la
stessa terna fuori, per confronto. Screenshot reale (§9, SwiftShader, HQ on),
e lettura del sorgente di three r170 vendorizzato (`WebGLRenderer`:
`renderTransmissionPass`, `WebGLRenderList.push`).

| dentro al vetro          | si vede? | perché |
|--------------------------|----------|--------|
| pezzo normale colorato   | **sì**, nitido e rifratto | è nella lista `opaque`, che il passaggio di trasmissione disegna nel suo target |
| pezzo emissivo           | **sì**, e il bagliore attraversa | opaco come sopra + il bloom selettivo composto sopra (§ viewer.js) |
| un altro vetro           | **solo come fantasma**: un anello, senza colore | dei vetri three ridisegna nel target solo le facce POSTERIORI (`side=BackSide`) dei materiali `DoubleSide`, campionando un target che contiene solo gli opachi |
| roba `transparent` (alpha): il vetro senza HQ, sprite delle targhette, overlay | **no** | `transmission>0 → transmissive`, `transparent → transparent`: la lista transparent non entra mai nel target |

Tre conseguenze che il piano deve rispettare:

1. **Vetro su vetro non funziona** e non si aggiusta con un parametro: è
   l'architettura a un solo passaggio di three. Per «vedere pezzi dentro altri»
   il pezzo ESTERNO va in vetro, quello interno NO (normale o emissivo).
2. **Sul telefono è peggio**: il ridisegno delle facce posteriori sta dentro
   `if (!WEBGL_multisampled_render_to_texture)`. Android Chrome quell'estensione
   ce l'ha, quindi lì il vetro interno sparisce del tutto. Da verificare sul
   moto g05 (`telefono_cattura`) prima di prometterlo.
3. **Il bagliore ignora la profondità**: il quad del glow è `depthTest:false`,
   quindi un emissivo dietro un pezzo OPACO alone lo stesso attraverso il muro.
   Per «vedere dentro» è persino comodo (fa da indicatore), ma va detto: non è
   una prova che il pezzo sia visibile.

E un dettaglio visto nello screenshot: il colore di un vetro si legge appena
(la sfera blu esce grigio-bruna sul fondo scuro). Il vetro dice «qui c'è un
guscio», non «questo è il pezzo blu».

---

## 1. ✂ Sezione — tagliare con un piano, campitura a righe oblique

### Cosa vede l'utente

Un bottone ✂ (tasto `X`) nella toolbar di /view. Si accende un piano di taglio
che passa per il centro della scena, normale a Z; tutto ciò che sta dalla
parte del semispazio scartato sparisce, e ogni pezzo tagliato mostra la
**faccia di taglio piena, campita a righe a 45°** (il tratteggio da disegno
tecnico, ANSI31), del colore del pezzo, più un contorno scuro. Due pezzi
diversi = due campiture distinguibili (angolo alternato 45°/135° o passo
diverso, come nei disegni d'assieme), così si capisce dove finisce la vite e
comincia il dado.

Controlli:

- **asse**: X / Y / Z / *vista* (piano perpendicolare alla camera, fissato al
  momento del clic) / *faccia* (tocca una faccia piana del pezzo → il piano ci
  si appoggia; riusa il fit di superficie del ✎ T, `fitSurface`);
- **posizione**: uno slider lungo la normale, limitato al bbox dei pezzi
  visibili, più una maniglia nel 3D (freccia sul piano, trascinabile come il
  gizmo dell'editor);
- **⇄ inverti** il lato tenuto;
- **per pezzo**: nella lista, un'icona ✂ accanto a ●/◎ — un pezzo escluso dal
  taglio resta intero (la vite intera dentro il dado sezionato: è la vista
  classica d'assieme e la più utile);
- stato nell'hash: `cut=z:12.5`, `cut=view:<quat>:<d>`, `cutflip=1`,
  `nocut=n3,n7.2` — un agente manda il link già sezionato, come già fa con
  `hide=`.

### Come si fa (viewer.js, così serve anche all'editor)

- **Il taglio**: `renderer.localClippingEnabled = true` e un `THREE.Plane`
  messo in `material.clippingPlanes` dei pezzi tagliati (non
  `renderer.clippingPlanes`: quello taglierebbe anche griglia, note e
  targhette, e non permetterebbe l'esclusione per pezzo). Il passaggio di
  trasmissione rispetta il clipping (`setGlobalState` è chiamato anche lì) —
  da verificare comunque a occhio su un vetro tagliato.
- **Il tappo** (la faccia piena): tecnica stencil standard (esempio three
  `webgl_clipping_stencil`): per ogni pezzo due passaggi senza colore che
  contano le facce anteriori/posteriori tagliate nello stencil, poi un piano
  grande sul piano di taglio disegnato solo dove lo stencil è dispari. **Per
  pezzo**, con stencil azzerato fra un pezzo e l'altro (o un `stencilRef`
  diverso): un solo stencil per tutta la scena darebbe parità sbagliate dove
  due pezzi si compenetrano — e invece vogliamo VEDERE l'interferenza: la
  zona dove due tappi si sovrappongono va evidenziata (rosso, righe incrociate)
  perché è materiale che due pezzi si contendono.
- **La campitura**: un `ShaderMaterial` sul piano del tappo, righe in
  coordinate DEL PIANO in mm (non dello schermo), così non «nuotano» quando si
  orbita e il passo resta leggibile come scala. Passo in px convertito in mm
  allo zoom corrente con l'arrotondamento a gradini, come la larghezza del
  pennello (`mmPerPx`), per non far scorrere le righe a ogni rotella.
- **Contorno**: il bordo del tappo è dove la mesh attraversa il piano — si
  calcola sulla CPU una volta per posizione del piano (intersezione triangolo /
  piano, segmenti in `LineSegments`). Serve anche per la quota (sotto).

### Trappole già note

- **La mesh deve essere chiusa** perché la parità dello stencil abbia senso.
  Le tessellazioni B-Rep lo sono (vertici duplicati per faccia ma nessun buco);
  un guscio aperto (`Join` di 5 facce, §5f), una curva, dei punti: niente
  tappo, solo il taglio. Riconoscerli: un pezzo `kind` curva/punti salta il
  tappo; un guscio aperto dà parità dispari fuori dal pezzo → tappo «che
  cola». Verificarlo con un test su `Join` aperto prima di dichiararlo.
- **Lato interno visibile**: i materiali sono già `DoubleSide`, quindi dentro
  il taglio si vedono le pareti interne — ok, ma vanno ombreggiate più scure
  (in un tappo che fallisce si vedrebbe l'interno «vuoto», e deve essere
  evidente che è un difetto, non un pezzo cavo).
- **Pick e penna**: `firstHit` deve ignorare la parte tagliata (il raycaster
  di three non conosce i clipping planes): filtrare i colpi nel semispazio
  scartato, altrimenti il ✎ disegna sull'aria e il clic seleziona un pezzo
  invisibile. Stessa cosa per ↔ Metro.
- **Animazioni**: con ▶ i pezzi si muovono e il piano no — il tappo va
  ricalcolato per frame solo se il pezzo si è mosso (lo stencil è gratis, il
  contorno CPU no: saltarlo durante il play, ridisegnarlo da fermi).
- **Il frame su richiesta** (`invalidate()`): ogni cambio di piano chiede un
  frame.

### Esatto, quando serve (fase 3)

Il tappo stencil è la sezione della TESSELLAZIONE: un foro di 3 mm è un
poligono. Per «capire il taglio» basta. Per misurarlo no: lì c'è già
`section_outline` (§7b) sul gen congelato — un bottone «sezione esatta» nel
pannello ✂ chiede il profilo vero al server e lo sovrappone (archi, raggi,
quote), e la nota dell'utente può portarsi dietro il piano (`cut` nella
nota) così l'agente legge esattamente la stessa sezione con
`cad_measure`/`section_outline`.

---

## 2. 🔍 Aspetto in /view — vetro, emissivo, colore per pezzo

/view oggi prende colore/finitura dal grafo congelato e non li fa cambiare.
Serve un modo SOLO DI VISTA (il gen resta immutabile) per dire «questo pezzo in
vetro, quello emissivo».

- Nella lista pezzi, un menu per riga (o tasto `G` sul pezzo selezionato):
  **normale / vetro / emissivo / fantasma** + colore. A livello di FOGLIA,
  come la visibilità, così un pezzo di una scena collide ha il suo.
- Hash: `look=n3:glass,n7.2:emissive:#ffcc00` — il link porta la vista.
- Un'azione composta, perché è quella che si vuole il 90% delle volte:
  **«Guarda dentro»** sul pezzo selezionato → lui resta com'è, tutti i pezzi
  che lo racchiudono (bbox che lo contiene) passano a vetro. Fa la cosa giusta
  da sola rispetto al §0: l'esterno vetro, l'interno opaco.
- **Fantasma** (nuova finitura, anche per l'editor): `transparent`, opacità
  ~0.15, `depthWrite:false`, più gli spigoli vivi (`EdgesGeometry`, soglia
  30°) opachi. È il «raggi X» dei CAD: l'ordine di disegno non è esatto
  (alpha), ma a 0.15 non si nota, e risolve ciò che il vetro non sa fare:
  - fantasma dentro fantasma si vede (vetro dentro vetro no);
  - funziona identico sul telefono (niente target di trasmissione);
  - costa quasi niente (il vetro HQ ridisegna la scena per ogni frame).
  Attenzione al §0 riga 4: un fantasma DENTRO un vetro non si vede. La regola
  da far rispettare all'interfaccia: in «Guarda dentro» si sceglie vetro
  oppure fantasma per tutto l'involucro, non un misto.
- Il bloom: `markGlow` va richiamato dopo ogni cambio (la maschera è per
  oggetto), e il glow attraverso i muri opachi (§0, punto 3) va bene così.

---

## 3. Fasi

1. **✂ base** in viewer.js + /view: piano su X/Y/Z, slider, inverti, tappo
   stencil per pezzo con campitura in coordinate del piano, contorno, hash,
   pick/penna/metro che ignorano il lato tagliato. Esclusione per pezzo.
2. **🔍 Aspetto**: menu per foglia, hash `look=`, finitura fantasma,
   «Guarda dentro». Verificare il §0.2 sul telefono vero.
3. **✂ avanzato**: piano «vista» e «faccia», maniglia 3D, evidenza
   dell'interferenza, sezione esatta dal server, `cut` nelle note e nei
   `cad_snapshot` (l'agente manda un link già sezionato: «guarda qui come la
   vite entra nel dado»).
4. **Editor**: stessa funzione in nodes.html (vive in viewer.js, quindi è
   soprattutto un bottone e il salvataggio in localStorage, mai nel grafo).

## 4. Da decidere prima del codice

- **Una o più sezioni?** Un piano solo copre quasi tutto; due piani (taglio a
  quarto, «cutaway») si fanno con gli stessi `clippingPlanes` +
  `clipIntersection`, ma lo stencil dei tappi diventa uno per piano. Proposta:
  uno nella fase 1, il quarto in fase 3 se serve.
- **Colore della campitura**: colore del pezzo (si capisce chi è chi) o grigio
  uniforme con colore solo al bordo (più «disegno tecnico»)? Proposta: colore
  del pezzo, righe scure.
- **Il taglio entra nella foto della nota?** Sì, se l'utente disegna su una
  vista sezionata: la foto (`takeView`) la mostra già, ma la nota deve dire
  anche il piano, o l'agente vede un buco che nel modello non c'è.

## 5. Dove mettere le mani

- `webui/viewer.js` — `makeMaterial` (fantasma), un modulo `section` (piano,
  stencil, campitura, contorno), `renderPreviews` (clippingPlanes per pezzo).
- `webui/view.html` — toolbar ✂/🔍, righe della lista, hash, `firstHit`.
- `cad_nodes/AGENT_HELP.md` — `cut=` e `look=` negli URL che l'agente manda.
- Test: `tests/test_generations.py` (hash, presenza dei controlli), un test
  `tests/ui/*.test.cjs` per l'intersezione triangolo/piano e la parità, e la
  prova nel browser sulla scena del §0 (vite nel dado: `bolt-and-nut`).
