# PLAN_MECHANISM — meccanismi cinematici: ingranaggi, cambi, leveraggi

Stato: **design + prova di fattibilità (punto 1)**. Discussione 2026-10-11.
Origine: feedback `20261011-180120-creepyfinger-v4` (gpunix) — Animate non sa
mostrare un meccanismo a giunti accoppiati.

## Il problema

`Animate` + `Motion` (§5d-ter) fa UN moto rigido con fase lineare/smooth in t.
Va bene per una rotazione pura (la falange media), non per un pezzo la cui posa
è una funzione NON lineare del driver: sul quadrilatero del dito l'angolo della
distale va 0° → 36° → 51° → 33° mentre la flessione va 0 → 45 → 90 → 140, e un
moto lineare stacca i pezzi dai perni a metà corsa. Un'animazione sbagliata è
peggio di nessuna quando la si usa per cercare interferenze. Ricalcolare il
grafo vero per ogni posa costa 18 s: lo slider è inutilizzabile.

E manca del tutto il caso più comune della meccanica: **muovo un pezzo, gli
altri collegati seguono** (ingranaggi, alberi, leveraggi, un cambio).

## L'idea: un `Mechanism` cinematico che si verifica con le collisioni

- **Driver** (uno o più): un grado di libertà dettato nel tempo — l'albero
  motore gira, la forcella del cambio scorre. Sono i `Motion` esistenti più
  un `KeyframeMotion` a tratti (proposta 2 del feedback).
- **Corpi + giunti**: rotoidale (asse + perno), prismatico, fisso, rigido
  (più pezzi solidali = un albero con le sue ruote).
- **Accoppiamenti**: coppia di ingranaggi (ω₂ = −z₁/z₂ ω₁), e in generale
  vincoli di chiusura (quadrilatero) risolti numericamente.
- **Uscita**: ogni corpo posato al `t` dello slider, con `_noodle_anim` di
  tipo `keys`. È la forma che `mesh_extractor._preview_of` trasforma GIÀ in una
  `Scene` con un corpo per pezzo (owner incluso): scrub a 60 fps nell'editor,
  ▶ e ≡ Tracce in /view, colore/finitura per pezzo — **frontend zero**.

### Ingranaggi che si agganciano da soli

Un `SpurGear` (evolvente) attacca al risultato i suoi dati (`z`, modulo,
asse, centro, larghezza). Due ingranaggi sono **in presa** quando: assi
paralleli, interasse ≈ m(z₁+z₂)/2, e le larghezze si sovrappongono lungo
l'asse. Allora rapporto = −z₁/z₂ e la fase iniziale del condotto è calcolata
perché i denti si incastrino (mezzo passo, funzione dell'angolo della linea
dei centri). Niente da dichiarare: "muovi uno, gli altri seguono".

### Il cambio viene gratis

L'aggancio dipende dalla sovrapposizione assiale, cioè dalla GEOMETRIA nella
posa corrente: si ricalcola a ogni fotogramma. Cambio a ingranaggi scorrevoli,
2 marce: primario con due ruote fisse, secondario con un gruppo che scorre sul
profilo scanalato (prismatico lungo l'asse, ruota con l'albero), driver 2 =
forcella con keyframe 1ª → folle → 2ª. In folle nessuna coppia si sovrappone →
il secondario sta fermo. Ogni marcia dà il suo rapporto senza casi speciali.

Un corpo non raggiunto da nessun driver nel fotogramma conserva l'ultimo
angolo (inerzia zero, nessun attrito: è cinematica).

### Collisioni: verifica, non motore

Far trascinare i denti dal contatto (pybullet) è fragile e lento. Il moto lo
decide la cinematica; le collisioni lo VERIFICANO: su N fotogrammi campionati
`manifold3d` interseca le coppie di corpi in movimento → report (chi, in che
intervallo di t, volume compenetrato) + un corpo rosso "interferenza" nel
fotogramma peggiore. È esattamente "dove sfregano" del dito, e sul cambio
mostra lo scontro dei denti a un innesto con la fase sbagliata (il motivo per
cui esiste il sincronizzatore).

### Leveraggi (il dito)

Stesso solutore: pose dei corpi come incognite, vincoli = giunti + chiusure,
risolti per fotogramma con `scipy.optimize.least_squares` partendo dalla
soluzione del fotogramma prima. Gli ingranaggi sono un vincolo lineare dentro
lo stesso schema.

### Lo sweep (proposta 1 del feedback) resta complementare

Una gen flip-book che esegue il grafo per ogni valore è esatta per QUALSIASI
parametro ma costa un'esecuzione per fotogramma (18 s sul dito). Utile più
avanti per ciò che non è moto rigido (crescita, varianti).

## Ordine

1. **Prova di fattibilità nel container** — `_mechanism` stand-alone: giunti
   rotoidali, aggancio automatico ingranaggi + fase, keyframe, controllo
   interferenze; misurare il costo per fotogramma su 3 ingranaggi e su un
   cambio a 2 marce. Nessun nodo nel catalogo ancora.
2. `SpurGear` (evolvente) + `Mechanism` / `Joint` / `KeyframeMotion` nel
   catalogo, helper nel PREAMBLE, test pure-Python.
3. Esempio `gearbox-2-speed`: albero motore + forcella su un solo slider,
   verifica che in folle l'uscita stia ferma.
4. Vincoli ad anello chiuso, provati sul quadrilatero di creepyfinger.

## Registro della prova (punto 1)

2026-10-11, `scripts/probe_mechanism.py` (numpy + manifold3d, niente
build123d; `docker exec -i noodle python - < scripts/probe_mechanism.py`).

**Treno di 3 ingranaggi** (z 20 → 30 → 15, m 2, il terzo a 60° dalla linea
dei primi due, per provare la fase con un angolo qualsiasi):
- costruzione dei 3 ingranaggi a evolvente in manifold: ~14 ms (1.7-3.3k tri);
- aggancio automatico corretto: (g20,g30), (g30,g15), nessun altro;
- rapporti esatti: g15 +480° per un giro dell'ingresso (= 360·20/15), g30
  −240° più i −6° dell'allineamento iniziale di fase (vedi sotto);
- **0,000 mm³ di compenetrazione su 120 fotogrammi di un giro completo** con
  gioco 0,05 mm; `min_gap` g20–g30 = 0,0495 mm, cioè il gioco impostato;
- controllo negativo: lo stesso g30 sfasato di mezzo dente → 146 e 132 mm³ di
  compenetrazione. Il controllo vede davvero i denti;
- costo: propagazione 0,06 ms/fotogramma, interferenza 1,3 ms/fotogramma.

**Cambio a ingranaggi scorrevoli, 2 marce** (primario 20t + 30t, gruppo
scorrevole 40t + 30t sul secondario, interasse 60 = m(20+40)/2 = m(30+30)/2,
forcella a keyframe 1ª → folle → 2ª):
- 1ª rapporto 0,5000, folle uscita ferma (0,000°), 2ª 1,0000: tutto dalla
  sovrapposizione assiale, nessun caso speciale;
- 7,5 ms/fotogramma con l'interferenza → 240 fotogrammi ≈ 1,8 s;
- 0 fotogrammi in collisione.

**B-Rep**: lo stesso profilo come `Polyline → make_face → extrude` in
build123d è valido, 183 ms (z 20) / 414 ms (z 40), volume uguale a quello di
manifold entro lo 0,01%. Quindi `SpurGear` può stare sulla corsia B-Rep
(esportabile in STEP, memo-cache), e il meccanismo lavora sulla sua mesh.

**Decisioni prese dalla prova**
- La fase d'ingranamento è una famiglia θ_b = θ_b0 + k·2π/z_b: si sceglie il k
  più vicino all'angolo attuale di b, così b resta continuo.
- Al fotogramma 0 b viene allineato (scatto ≤ mezzo passo): è il montaggio, va
  fatto PRIMA del fotogramma 0 e non contato come innesto.
- **Innesto durante il moto: `clash` (scelta di quill, 2026-10-11)**, `snap`
  resta come opzione. Lasciare il condotto sfasato e basta sarebbe sbagliato
  (collisione per tutta la marcia); un innesto vero senza sincronizzatore
  sbatte mentre entra ed è in fase a innesto completo. Quindi l'errore di fase
  si recupera in proporzione alla sovrapposizione assiale: il secondario resta
  continuo (nessuno scatto) e i denti si compenetrano solo durante la corsa
  d'ingresso. Misurato sul cambio: errore 6°, 21 fotogrammi in collisione
  (t 0,562–0,646, la corsa della forcella da 10 a 18 mm), picco 44 mm³ a metà
  corsa, poi 0. Con `snap`: 0 fotogrammi in collisione e uno scatto di 6°.
  Rapporti e folle identici nei due modi.
- Un corpo che nessun driver raggiunge tiene l'ultimo angolo (il folle).

## Registro del punto 2 (2026-10-11)

Nodi (categoria `mechanism`, catalog.py §12e; runtime nel PREAMBLE):

- **`SpurGear`** — evolvente B-Rep (`Polyline → make_face → extrude`), centrato su
  `at` con la larghezza centrata lì, asse x/y/z, `backlash`, `phase`. I dati del
  dente (`_noodle_gear`) stanno sulla forma in coordinate MONDO insieme alla
  Location che aveva alla costruzione: il meccanismo ricava la posa attuale come
  `L_now · L_built⁻¹`. Verificato nel container: `Pos*`, `Rot*`, `moved`, `rotate`
  conservano sia la Location sia l'attributo; una Union li perde (forma nuova).
  Per questo `at` e non `origin`, come Thread.
- **`KeyframeMotion`** (label «Keyframes») — `0:0, 1:360, …` o due liste
  `times`/`values`; `linear | smooth | step`. Dati puri come Motion.
- **`Joint`** — un corpo rigido (tutte le forme del collector), asse `auto` =
  quello del primo ingranaggio, pivot = il suo centro. `turn`/`slide` accettano
  Keyframes, Motion (componente lungo l'asse) o un numero; `rpm` senza piano;
  `follows` + `ratio` (scanalato = 1, cinghia = Ø/Ø, inversione = −1);
  `kind fixed` = telaio. Un ingranaggio fuori asse è un ERRORE, non un moto
  sbagliato.
- **`Mechanism`** — `_emit_mechanism` (come `_emit_orient`): una soluzione, tre
  uscite — `result` (i corpi posati, lista → Scene con `keys` per corpo e owner
  = nodo che li ha disegnati), `report`, `clash` (mesh della compenetrazione al t
  corrente). Il valore restituito porta anche `trace` (tempi, gradi, mm per
  corpo, innesti, conflitti, collisioni) — la tabella cinematica, per agenti e
  test.

Cose trovate facendolo:

- **Il fantasma a riposo.** La regola automatica dell'occhio considera
  terminale un nodo che alimenta un ingresso non geometrico, e l'uscita di Joint
  è `data`: ingranaggi e alberi venivano disegnati ANCHE a riposo, sotto la
  copia posata. Si vedeva solo sui corpi che scorrono (a t=0,9 il gruppo
  compariva due volte); quelli che ruotano su sé stessi si sovrapponevano
  identici. `_GEOMETRY_CARRIERS = {"Joint"}` nel transpiler; test che pinna che
  nell'esempio disegna solo `mech`.
- I versi: la fase si misura attorno all'asse del PRIMO ingranaggio della coppia
  e ogni corpo porta il suo segno (asse del giunto · asse della coppia), così due
  assi antiparalleli non specchiano il moto (test).
- Un anello dispari di ingranaggi (tre ruote uguali a triangolo) non può girare:
  il report scrive `CONFLICT`, non lo nasconde.
- Frontend: solo `Mechanism` in `TIMELINE_NODES` (nodes.html). La Scene, lo
  scrub, ▶/Tracce in /view, colore per pezzo erano già generici.

Misure sull'esempio `gearbox-2-speed` (container locale, 21 nodi): esecuzione
completa ~10 s a freddo, `_mechanism` 1,8 s (481 keyframe + 161 fotogrammi di
collisione); report: 1ª −0,5000, innesto 2ª a t=0,571 con −3,00° di errore,
urto dei denti 10 fotogrammi (t 0,575–0,631) picco 14,6 mm³. Screenshot
frontali a t=0,25 / 0,6 / 0,9 guardati: 1ª in presa, 2ª in ingresso, 2ª in
presa, nessun doppione. Gen `gearbox-2-speed/g1`: timeline 8 s riconosciuta.

Test: `tests/test_mechanism.py` (22, pure-Python: rapporti, denti alternati a
qualsiasi angolo della linea dei centri, assi antiparalleli, follows, rpm,
conflitto, cambio 1ª/N/2ª in clash e snap, gioco, keyframes, emissione,
niente fantasma). `tests/fixtures/node_sizes.json` + i 4 nodi, catturati dal
browser nel container.

Aperto per il punto 3/4: la cremagliera e gli ingranaggi interni (la fase
cambia formula), i satelliti (ingranaggio non coassiale col proprio giunto), e i
vincoli ad anello chiuso del dito.

## Registro del punto 4 (2026-10-11) — anelli chiusi, il dito di creepyfinger

Due cose nuove, nient'altro:

- **`Joint.parent`**: perno e asse si danno nella posa di riposo (mondo) e poi
  viaggiano col corpo genitore. La posa di un corpo è quella del genitore
  composta con la sua; i genitori in ciclo sono un errore che dice di usare un
  Pin.
- **`Pin`** (`a`, `b` = due Joint, `b` vuoto = il telaio, `at` = il punto a
  riposo): un punto che due corpi condividono. È il vincolo che chiude un
  anello. Per fotogramma: driver → propagazione (ingranaggi, follows) → i corpi
  ancora liberi legati a un Pin (e i loro antenati liberi) si risolvono insieme
  con Gauss-Newton smorzato (Jacobiano numerico, passo limitato a 0,5 rad),
  partendo dagli angoli del fotogramma prima — è questo che tiene il leveraggio
  sul ramo in cui è stato montato — → seconda propagazione dai corpi risolti.
  Se un perno non chiude (scarto > 1 µm) il report scrive `LOOP OPEN` con
  l'intervallo e lo scarto peggiore.

Già che c'era, la fase degli ingranaggi si misura sulle direzioni VERE dei denti
nella posa corrente (non più su angoli di riposo + segno): così vale anche per
un ingranaggio su un corpo che si muove. Test: un satellite sul portatreno
attorno a un solare fisso gira, rispetto al portatreno, di +zs/zp.

**Il dito.** Quadrilatero da `cinematica.json`: media attorno a J1 (0, 12),
distale su J2 (55, 12) con parent la media, biella fra A (2, 3) sul guscio e
B (43, 16,5) sulla distale. Risolto contro la tabella `fl2` dell'altro agente:
identico alla terza decimale a 20/45/90/120/140° (18,787 / 36,308 / 51,034 /
44,625 / 32,887), e al ritorno resta sullo stesso ramo. Sotto zero NON
coincide, e ha ragione il solutore: **sotto −13,9° di flessione il
quadrilatero passa il punto morto e non chiude** — a −14° e −15° nessun angolo
della distale rimette la biella alla sua lunghezza (scarto minimo 0,019 e
0,177 mm); la tabella lì è un valore bloccato a −30°, e l'interpolazione fra
−14 e −13 dava l'errore di 1,18°. Il campo dichiarato del dito (−15…140) va
oltre quello del leveraggio.

**Collisioni: la posa di riposo come riferimento.** Il primo report diceva
«Guscio × Falange media, 91 fotogrammi su 91, picco 220 mm³» — vero e inutile:
misurato pezzo per pezzo, è la squadretta dentro l'ingombro del servo (138 mm³
a ogni angolo) più il mozzo della media nel guscio, già a flessione 0. Ora ciò
che si compenetra nella posa di riposo (tutti gli angoli a 0 = il montaggio
com'è modellato) è elencato a parte («designed contact, or a CAD clash to
fix»), con l'intervallo durante il moto; «collisions while moving» elenca solo
le coppie che a riposo non si toccano. Sul dito: nessuna collisione in
movimento su 91 fotogrammi; 220,5 mm³ a riposo (211–221 in movimento).

Altri fantasmi: un `ConstructPoint` che alimenta solo un Pin veniva disegnato
fermo nella posa di riposo mentre il perno vero si muove → `Pin` in
`_GEOMETRY_CARRIERS`. Un CodeBlock che alimenta solo CodeBlock passanti resta
disegnato (ingressi dati): nel grafo del dito ha l'occhio spento a mano.

Grafo di prova: `projects/creepyfinger-mech` (solo sul container locale; i BREP
copiati dal gpunix in `moto/`): un CodeBlock che importa i pezzi a flessione 0,
un passante colorato per pezzo (l'owner di ogni corpo della Scene è il nodo che
lo disegna), 4 Joint, 1 Pin, Keyframes «0:0, 3:140, 4:140, 6.5:−13, 7.5:0».
Mechanism 1,6–1,8 s (451 keyframe, 91 fotogrammi di collisione), esecuzione
6,6 s. Screenshot guardati a t 0 / 0,2 / 0,4 / 0,75: la biella resta sui due
perni. Gen `creepyfinger-mech/g1`.

Test (29 in `tests/test_mechanism.py`): chiusura contro un oracolo
indipendente (bisezione sulla lunghezza della biella), ritorno sul ramo,
punto morto a −13,9° riportato, genitori in ciclo, Pin senza punto, satellite,
Pin fra i portatori.

Aperto: cremagliera e ingranaggi interni; giunti prismatici dentro gli anelli
(biella-manovella: il Pin c'è, manca solo il caso nel solutore — `slide` oggi
è sempre un driver); ruote coniche (assi non paralleli).
