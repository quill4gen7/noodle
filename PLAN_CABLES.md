# PLAN_CABLES — cavi dentro il pezzo: quanto spazio prendono, e se sono troppo pressati

Stato: **indagine + prototipo misurato, provato su creepyFinger v4, nessun nodo**. 2026-10-09/10.
Prima prova (scatola con feritoia, XPBD) nei §2–4; la prova sul pezzo vero, che ha cambiato motore, nel §10.
Prototipo: `scripts/proto_cables_xpbd.py` (gira nel container, numpy + scipy +
manifold3d, niente dipendenze nuove). Risultato da guardare:
`/view/zz-cavi-probe/g1` (tre scatole, cavi rilassati, tagliate a metà).

## 1. La domanda

quill: «simulare e calcolare lo spazio occupato dai cavi — tubi di diametro
regolabile, più o meno flessibili, e le loro collisioni per capire se sono troppo
pressati».

Le domande a cui il nodo deve rispondere sono queste, in ordine di frequenza:

1. **Ci passano?** Dal connettore A al connettore B, attraverso quella
   feritoia o quel canale, con quel diametro.
2. **Dove premono, e quanto?** Dove si schiacciano fra loro o contro le pareti.
3. **Curvano troppo?** Ogni cavo ha un raggio minimo, tipicamente 4–10×Ø.
4. **Quanto sono lunghi, e quanto volume occupano?** Questo serve per la
   distinta e per lo scomparto.

## 2. Due livelli

### Tier 0 — statico, senza simulazione (giorni)

Il cavo segue il percorso che gli dai (connettori + punti di passaggio, una
`Spline`). Diventa un tubo sulla mesh lane, perché un manifold3d hull di sfere
consecutive è veloce, mentre lo sweep OCCT seguito da booleane non lo è
(§5c, §5g).

| Controllo | Come |
|---|---|
| Intersezione cavo–pezzo | manifold3d (`^`), volume e posizione |
| Intersezione cavo–cavo | distanza fra polilinee meno i raggi; nessuna booleana |
| Raggio minimo di curvatura | curvatura discreta lungo il percorso |
| Riempimento di un canale | Σ π r² dei cavi che attraversano la sezione, diviso l'area libera della sezione (`slice` di manifold3d, come il piano debole di §5d). Le norme di impianto usano ~40 % oltre due cavi: è la soglia predefinita |
| Lunghezza e volume | somme |

Questo livello basta per «c'è posto?», ma non dice **come si sistemano** i cavi.
Un percorso disegnato a mano attraversa il fascio e non si appoggia sul fondo.
Per questo serve il Tier 1.

### Tier 1 — i cavi si rilassano (XPBD), *misurato*

I cavi partono dal percorso del Tier 0 e si assestano sotto gravità. Gli estremi
sono fissati ai connettori, il cavo ha la sua rigidezza, e urta il pezzo e gli
altri cavi. Il risultato è la loro forma reale, insieme a sovrapposizioni e
forze di contatto.

**Modello** (Müller 2007 PBD, Macklin 2016 XPBD — è lo schema dei solver di
cavi nei giochi e nei simulatori chirurgici):

- **Il cavo** è fatto di particelle a passo r (sfere di raggio r a passo r: un
  tubo continuo).
- **Lunghezza:** un vincolo rigido fra particelle vicine.
- **Flessione:** un vincolo vettoriale C = x₀ − 2x₁ + x₂ con cedevolezza s³/EI.
  EI è vero e si misura in N·m². Un cavo di alimentazione Ø6 in PVC sta fra
  1e-3 e 1e-2 N·m²; un filo singolo è ~10× più morbido; un corrugato è più rigido.
- **Contatto col pezzo:** SDF su griglia, campionata con interpolazione
  trilineare e gradiente.
- **Contatto fra cavi:** coppie di sfere trovate con `cKDTree.query_pairs`. Lo
  stesso cavo è escluso entro 3 particelle.
- **Pressione:** il moltiplicatore λ del contatto, diviso dt², è una **forza**
  (XPBD la dà gratis). Si legge anche la sovrapposizione residua in % del
  diametro.

**Misure** (numpy puro, nel worker, 1,2 s simulati, 24 substep a 60 fps):

| Scena | particelle | sim | sovrapp. cavo–cavo | stiramento | F max |
|---|---|---|---|---|---|
| 4×Ø6, feritoia 14×14 (ci stanno) | 178 | 0,8 s | 1 % Ø | 0,2 % | 8× peso del tratto |
| 4×Ø6, feritoia 10×10 (non ci stanno) | 178 | 0,8 s | **28 % Ø** | **17 %** | **230×** |
| 16×Ø6, 26×26 (al limite: 4×4 = 24) | 800 | 2,3 s | 8 % | 7 % | 59× |
| 16×Ø6, 22×22 (no) | 796 | 2,3 s | 15 % | 8 % | 108× |
| 32×Ø4, 50×30 | 2600 | 7,1 s | 6 % | 15 % | 56× |

SDF: voxel 0,02 s, distance transform 0,25 s su 150×70×50 celle. Nel caso dei 4
cavi la distinzione è netta (1 % contro 28 %, 8× contro 230×). Nella feritoia
14×14 i cavi si dispongono 2×2: verificato sulle coordinate e a vista.

## 3. Cosa è stato scartato, e perché (misurato)

**pybullet, cavi come catene di capsule** (è il motore che `Drop` già usa,
`useMaximalCoordinates=True` per la trappola dei 100 mm/s di §5h).

- È lento: 3,7 s per 1,5 s simulati, perché la molla di flessione va applicata
  da Python, segmento per segmento, a ogni passo.
- Soprattutto, **sotto pressione la catena si strappa invece di schiacciarsi**:
  i giunti si aprono fino a un diametro intero (6 mm). Il risultato è un cavo
  interrotto, non una misura di quanto è pressato.
- Rimane utile per una sola cosa: il contatto capsula–mesh concava, che però la
  SDF fa meglio per questo uso.

## 4. Trappole trovate nel prototipo

1. **`_voxelize` (flood-fill, §5h) riempie le cavità chiuse.** Per lui l'interno
   di una scatola chiusa con il coperchio è «pieno», cioè proprio lo spazio dove
   stanno i cavi. Il prototipo voxelizza invece per **parità lungo z**
   (`voxel_parity`), che vale per qualsiasi mesh watertight e costa uguale
   (0,02 s). Va messa nel PREAMBLE accanto a `_voxelize`, non al suo posto: alla
   galleria del vento serve proprio il flood-fill.
2. **Partire con il diametro pieno rompe i casi stretti.** I percorsi convergono
   tutti al centro della feritoia, quindi 16 cavi partono sovrapposti in un
   punto. Il risolutore li separa tirando sulla lunghezza: **204 % di
   stiramento, F 474×**, in un caso dove i cavi ci stanno. **Gonfiare** il
   raggio da 0,3r a r nel primo 60 % della simulazione porta a 7 % e 59×. Il
   gonfiaggio è obbligatorio.
3. **Il Jacobi a 2 iterazioni lascia del 7–15 % di stiramento** anche quando i
   cavi ci stanno. Lo stiramento è un buon segnale di «non ci stanno», ma solo
   se è ~0 quando ci stanno. Servono più iterazioni sulla lunghezza, oppure un
   Gauss-Seidel per colori (pari/dispari: due passate vettorizzate). Il
   rapporto va ri-misurato dopo.
4. **Connettori sovrapposti = errore, prima di simulare.** Il prototipo, con 16
   cavi messi a 3 mm l'uno dall'altro, partiva con estremi fissi già
   compenetrati. Il risultato era 50 % di sovrapposizione «misurata»; in realtà
   era colpa dell'input.
5. **La memoria della SDF cresce con l'involucro.** Con passo 0,75 mm, una
   scatola 300×200×100 mm è circa 14 M celle, cioè ~230 MB con il gradiente.
   Le alternative sono una banda stretta attorno al pezzo (fuori dalla banda =
   libero), oppure un passo legato al diametro più piccolo (h ≈ Ø/6) e una
   griglia solo attorno al fascio di percorsi.
6. **Unità.** Il prototipo dà la forza in «volte il peso del tratto», che basta
   per confrontare ma non dice niente a chi progetta. Con ρ e EI veri, presi da
   un preset di cavo, λ/dt² diventa newton per millimetro di cavo. Il verdetto
   va dato su soglie di sovrapposizione e di raggio, non sui newton.

## 5. Nodi proposti

| Nodo | Cosa fa |
|---|---|
| `Cable` | Un cavo. Ingressi: `start`, `end` (punto, e opzionalmente un piano = direzione d'uscita del connettore), `via` (lista di punti/clip, che possono essere fissi), `diameter`, `stiffness` (preset: filo, cavo, corrugato, oppure EI), `slack` % o `length`. Uscita: un **piano**, come `Wind`/`Motion` (un dict, zero geometria), più l'anteprima del tubo lungo il percorso grezzo |
| `CableRoute` | Tier 0. Cavi + ostacoli → tubi (mesh) e report (interferenze, raggio minimo, riempimento per sezione, lunghezze) |
| `CableSim` | Tier 1. Cavi + ostacoli → tubi **rilassati** (lista di mesh, una per cavo, quindi in fan-out) e report. Seconda uscita: i **punti pressati** come mesh a sé, come `OverhangFaces` (§5d), così hanno un colore loro senza toccare il frontend |
| `CableSection` | Opzionale. Il riempimento in un piano scelto, con il disegno della sezione. Riusa `section_outline` |

Il report va in un `Panel`, nello stile di `PrintCheck`:
- per cavo: lunghezza, raggio minimo contro il limite, sovrapposizione massima
  con il pezzo e con gli altri, F massima e dove;
- in fondo: il verdetto **ci stanno / al limite / troppo pressati**.

Una sola risoluzione per due uscite: `_emit_cablesim`, modellato su
`_emit_orient` / `_emit_windtunnel`.

## 6. Collegamenti con quello che c'è già

- **/view ✎³ penna 3D e ⊞ Piano** disegnano già tubi nello spazio, e un tratto
  di nota ha posizione e spessore in mm. Una nota «i cavi passano di qui»
  disegnata dall'utente può diventare i `via` di un `Cable`: l'agente la legge
  con `cad_notes`.
- **↔ Metro** misura la feritoia dove l'utente dice che è stretta.
- **Animazione:** i cavi si deformano, quindi non sono un `kind:"keys"` rigido.
  La v1 dà solo il risultato finale. Un replay vorrebbe vertici per fotogramma
  (lo terrei fuori).
- **Memo:** la simulazione è deterministica, senza `random`, quindi è
  cacheabile come la galleria del vento.

## 7. Fasi

1. `voxel_parity` + SDF a banda stretta nel PREAMBLE, con test sulla scatola
   chiusa (cavità vuota, pareti piene). *½ giorno*
2. `Cable` + `CableRoute` (Tier 0), con esempio `examples/cable-slot.json`
   (la scatola del prototipo). *1–2 giorni*
3. `CableSim`: il prototipo ripulito, Gauss-Seidel per colori, gonfiaggio,
   unità vere, controllo dei connettori, uscita «punti pressati». La soglia del
   verdetto va tarata sulle tre feritoie del prototipo più una vera. *2–3 giorni*
4. AGENT_HELP, topic `cables`; CLAUDE.md §5i. *½ giorno*

## 8. Fuori, per ora

- **Cavi piatti (flat/ribbon)**: non sono tubi. Servirebbe un vincolo di
  torsione e una sezione rettangolare.
- **Fasci legati da fascette**: un `via` condiviso fra più cavi lo approssima.
- **Compressione vera della guaina** (FEM): la sovrapposizione residua ne fa
  da indicatore.

## 9. Domande per quill (prima tornata)

- **Quali cavi**: alimentazione tondi, servo (tre fili affiancati: quasi
  piatti), corrugati?
- **Verdetto**: basta ok / al limite / troppo pressati, o servono newton veri?
- **Pezzo di prova**: c'è un pezzo reale su cui tarare le soglie? Per esempio
  tars-pet / sg92r, che ha servo e cavi.

## 10. Prova su creepyFinger v4 (2026-10-10) — e il motore cambia

quill: «provalo con il creepy finger; accoppiare i cavi per le piattine; la forza vera non importa: una
**lunghezza utile** e sapere **quando si schiacceranno e come si piegheranno** chiusi dentro l'oggetto».
Codice: `scripts/cavi/` (`run_dito.py` esegue il CodeBlock e esporta le mesh, `cavi2.py` scena/geodetica,
`cavi3.py` energia, `dito3.py` chiusura, `dito_chiuso.py` lunghezze a guscio chiuso). Gen sul gpunix:
`zz-cavi-creepyfinger/g1` (a libro), `g2`/`g3` (dall'alto, due lunghezze), `g4` (chiuso, cavi stesi).

**Ipotesi** (da confermare): servo = piattina di 4 fili Ø0,9 che esce dal lato corto a x = sx1, larga
lungo z (il «3 + il blu, 3,5 × 1,5» del codice); batteria = rosso/nero Ø1,2 accoppiati, sale nella
camera davanti alla batteria e va al JST; GY-521 = piattina di 4 fili Ø1,0 dai pin −y verso il basso;
servo e GY-521 arrivano su piazzole sotto la scheda a y = ∓9,5, x −24…−31,6 (ai bordi c'è la guida:
la fila di pin vera a ±11,4 è dentro il pieno). EI 1e7 g·mm³/s² (= 1e-5 N·m², filo in PVC).

### Cosa ha imparato il prototipo (e perché XPBD è uscito)

1. **XPBD non regge un filo vero.** Un filo da 26 AWG si regge da solo per ~10 cm: la flessione è
   rigida rispetto al peso, e con i vincoli quasi rigidi il Jacobi non converge. Misurato, senza
   ostacoli: un filo di 40 mm fra estremi a 30 mm restava stirato del 19 %. Non è taratura.
2. **Ci interessa l'equilibrio, non la dinamica.** `cavi3.py` minimizza l'energia (flessione
   (EI/s)(1 − cos θ), molla di lunghezza, legami di piattina, gravità, penalità di contatto su SDF e
   fra sfere) con L-BFGS; la chiusura è quasi-statica, a passi. Gradiente verificato (errore 2e-8);
   filo singolo e piattina a vuoto: stiramento 0,003 %.
3. **La partenza conta:** dritta e compressa è un punto di sella (resta lì); serve una gobba col lasco
   PERPENDICOLARE alla piattina (dove piega facile), scelta fra lobi e direzioni quella che tocca meno.
4. **Piattina sfilata ai capi:** se i fili vanno a pin a 2,54, i tratti sfilati sono più lunghi del
   tratto incollato (fanno la diagonale). Senza, il filo esterno risultava stirato del 17 %.
5. **Passo della chiusura < diametro del filo.** A 6° per passo il bordo del coperchio si sposta di 4 mm,
   il filo passa da una parete di 1,8 mm e la penalità lo spinge FUORI dal lato sbagliato. Passi di
   0,4 mm (o 1,5°) e partenza calda: le particelle vicine al coperchio si muovono con lui.
6. **Il verso della cerniera** si controlla a 90°, non a 180° (a 180° i due versi coincidono): il primo
   giro apriva il coperchio attraverso il corpo.
7. **Gli attacchi falsano le misure** (la particella d'estremo tocca la faccia del JST/servo): penetrazione
   e stiramento si misurano lontano 3 particelle dagli attacchi.

### Risultati (con le ipotesi sopra)

**Lunghezza minima** = percorso più corto nello spazio libero (BFS 26-connesso su griglia 0,4 mm, poi
accorciato), per posa:

| posa | servo | batteria | GY-521 |
|---|---|---|---|
| chiuso | 43,4 | 39,1 | 31,8 |
| aperto a libro (cerniera sullo spigolo −y del taglio) 90° | 48,0 | 53,4 | 28,6 |
| a libro 180° | 49,5 | 63,8 | 28,6 |
| coperchio alzato dritto 20 mm | 52,2 | 47,9 | 28,6 |
| alzato 30 mm | 56,9 | 55,9 | 28,6 |

**Quanta ne sta, chiuso, col cavo steso nel suo percorso** (`dito_chiuso.py`, +mm sul minimo):
batteria **ci sta fino a +30 (69 mm)**, l'avanzo si ripiega nella gabbia, R min 4–6 mm; GY-521 ci sta a
+5 (37 mm), da +10 l'avanzo non ha posto sotto il modulo; servo **non conclusivo** (schiacciato a ogni
lunghezza: la piattina deve girarsi di taglio per entrare nel passaggio −y largo 3,0 mm, e il modello
non ha rigidezza a torsione).

**Chiusura** (quando si schiaccia):
- **a libro, senza infilare**: la piattina del servo resta FUORI, pizzicata sul bordo −y (si schiaccia
  da 16°); è un risultato vero sul gesto, non sulla lunghezza.
- **dall'alto 30 mm**: batteria 59 mm chiude pulita (mai schiacciata); a 66 e 76 mm il lasco penzola e
  resta preso a 3° dalla chiusura → l'avanzo va STESO nella gabbia prima di chiudere, da solo non ci va.
  Servo e GY-521 schiacciati a ogni lunghezza: un cavo sollevato fuori dal suo canale non ci ricade da
  solo, il guscio è pieno con vuoti stretti.

**Quindi la lunghezza utile della batteria** (aprendo di 30 mm dall'alto) è **56–69 mm**, a patto di
stenderla nella gabbia; aprendo a libro fino a 90° servono ≥ 54 mm.

### Cosa cambia nel piano

- Il Tier 1 è **minimizzazione d'energia quasi-statica**, non XPBD. Il prototipo XPBD (`proto_cables_xpbd.py`)
  resta per la scatola con feritoia, dove funzionava perché i cavi erano morbidi e lo spazio largo.
- Il numero più utile è la **geodetica**, ed è economico (~8 s di griglia per tre cavi): «lunghezza
  minima per aprire così» non chiede nessuna simulazione. Va nel Tier 0.
- La chiusura simulata dice **dove** un cavo viene pizzicato se lo lasci dov'è; quello che ci sta va
  chiesto a guscio chiuso col cavo già steso (`dito_chiuso.py`). Sono due nodi/due domande diverse.
- Manca: **torsione** nella piattina (senza, una piattina che deve girarsi di taglio non converge);
  attacchi con la loro direzione e una piccola zona libera garantita; percorso guidato da punti `via`
  (i canali del progetto) invece della sola geodetica.

### Domande (seconda tornata)

- Dove arrivano davvero servo e GY-521 sulla scheda (pin, lato, da sopra o da sotto)?
- Il GY-521 ha i pin verso il servo (giù)? Sopra il servo restano 3 mm: è lì che si schiaccia.
- Come chiudi davvero: dall'alto, a libro, o la scheda entra da dietro col coperchio già chiuso?
