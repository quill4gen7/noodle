# PLAN — fluidi: vento, medium, galleria del vento

Successore dettagliato di **`PLAN_SIM.md` §B**, che resta valido come inquadramento
ma che questo documento **corregge su un punto sostanziale** (§1). Misure prese il
2026-07-27 dentro il container `noodle` (12 core, nessuna GPU).

> **Stato: livelli 1 e 2 IMPLEMENTATI** (2026-07-29). Il livello 3 (particelle vive
> nel viewer) resta fuori, per scelta. Cosa è stato costruito, cosa è costato più del
> previsto e cosa NON è arrivato: **§7, in fondo.** Le tre premesse di §1–§2 sono state
> ri-misurate prima di cominciare e reggono tutte; quello che il documento dava per
> scontato e non lo era è il **Cd** (§7c).

---

## 1. La correzione: non serve né FluidX3D, né una GPU, né un'immagine separata

`PLAN_SIM.md` §B sceglieva come Tier 1 **FluidX3D (OpenCL, GPU) pilotato come
servizio**, relegando il "mini-LBM numpy interno" ai domini piccoli senza
dipendenze; §C3 ne deduceva un'immagine `noodle-sim` opzionale con un profilo
compose separato.

**Misurato, quella deduzione cade.** Un D3Q19 in numpy puro, dentro il worker che
già esiste, gira a **19 ms/step** su una griglia 96×32×32 — cioè **~17 s** per una
galleria del vento completa da 900 step. Con la memo-cache (§5e/§transpiler) quel
costo si paga solo quando cambia davvero la geometria o il vento. Per la v1:

- **niente OpenCL, niente GPU, niente immagine separata** — `numpy` e `scipy` sono
  già nell'immagine base;
- **niente coda di job asincroni** (`PLAN_SIM.md` §C1): 17 s stanno comodamente
  dentro il `timeout` dell'executor, e il nodo si illumina da solo nell'editor
  perché `_ev()` bracchetta ogni nodo in modalità memo. C1 resta un prerequisito
  della **FEA**, non della galleria.

FluidX3D e OpenFOAM restano dove `PLAN_SIM.md` li aveva messi: Tier 2, da tirare
fuori solo se servirà fedeltà da certificazione.

## 2. Le misure

Tutte dentro il container, `PYTHONPATH=/app`, float32.

| cosa | misura |
|---|---|
| LBM 2D D2Q9, 200×100 | **2,8 ms/step** → 3000 step = 8,5 s |
| LBM 2D D2Q9, 400×200 | 12,2 ms/step → 3000 step = 37 s |
| LBM 3D D3Q19, 64×32×32 | **11,6 ms/step** → 2000 step = 23 s |
| LBM 3D D3Q19, 96×48×48 | 57,0 ms/step → 2000 step = 114 s |
| LBM 3D D3Q19, 128×64×64 | 258,3 ms/step → 2000 step = 517 s |
| galleria end-to-end, 96×32×32, 900 step | 17 s |

**Lo scaling è superlineare** (11,6 → 57 → 258 per 8× di celle ciascuno): oltre
~100k celle si esce dalla cache e il costo esplode. È il vincolo di progetto vero —
**il dominio va tenuto sotto ~100×48×48**, non allargato "tanto è veloce".

### 2a. La voxelizzazione era il rischio, e la strada ovvia è quella sbagliata

Il ponte geometria → solver sembrava banale e invece è dove si perde tutto. Usando
`_winding_inside` (già nel PREAMBLE, §5e) su ogni cella:

| griglia | winding number | guscio + flood-fill |
|---|---|---|
| 64×32×32 | 8,65 s | **0,017 s** |
| 128×64×64 | 62,45 s | **0,079 s** |
| 192×96×96 | — | 0,183 s |

**500× di differenza, e il solver costava meno del ponte.** La strada giusta:
rasterizzare il *guscio* (`trimesh.remesh.subdivide_to_size` fino a lato < 0,7 celle,
poi timbrare i vertici nella griglia) e riempire con un flood-fill dall'esterno
(`scipy.ndimage.label` + le sei facce del bordo come semi). Verificato: **accordo
100%** col winding number, superset di ~un voxel (345 celle su 64×32×32) — che è
esattamente ciò che il bounce-back vuole, perché la parete sta *sulle* celle solide.

`_winding_inside` resta giusto per quello per cui è nato (l'oracolo punto-in-mesh di
`PopulateGeometry`, poche migliaia di punti sparsi). Non è un voxelizzatore.

### 2b. La fisica esce giusta

Provato end-to-end su parti build123d vere, dominio 96×32×32, 900 step, u₀ = 0,06
in unità reticolo, bounce-back sul solido, inlet all'equilibrio e outlet copiato:

```
sfera (tozza)           scia sull'asse u/u0 = +0.09   vmax 1.57×u0
goccia (aerodinamica)   scia sull'asse u/u0 = +0.52   vmax 1.13×u0
```

Scia morta dietro il corpo tozzo, flusso ancora attaccato dietro quello affusolato;
e il corpo tozzo accelera molto di più il fluido che lo scavalca. È il risultato da
manuale — **prima di credere ai tempi, ho verificato che il campo fosse fisica**.

Nota su come *non* misurarla: la prima lettura mediava la velocità su tutta la
sezione a valle e dava `u/u0 = 1.00` per entrambi, cioè "nessuna scia". Falso: la
media includeva l'anello di fluido *accelerato* attorno al corpo, che compensa
esattamente il deficit al centro. **La scia si legge sull'asse**, non in media.

## 3. I tre livelli, in ordine di costo

### Livello 1 — vento e medium dentro `Drop` (nessun solver)

È la richiesta nella sua forma minima: *un emettitore di vento e la scelta del fluido*.
Non serve CFD — servono due forze in più dentro `_dyn_sim`, che già gira pybullet.

- **`Wind`** (categoria `print`, accanto a `Motion`): direzione + velocità + un
  `kind` — `uniform` / `jet` (cono da un punto, con apertura e caduta 1/r²) /
  `vortex`. Emette un piano-dato `{kind, dir, speed, …}` esattamente come
  `ContainerMotion` emette il suo, e si attacca a un socket `wind` di `Drop`.
- **`medium`**: un parametro select su `Drop` — vuoto / aria / acqua / olio / miele —
  che fissa ρ e μ. Non è cosmetico: cambia sia il drag sia Archimede, e i bulloni di
  `threaded-jar-pour` che cadono nell'acqua invece che nell'aria sono un esempio che
  si vede a occhio.
- **Le forze**, per corpo e per step, con `applyExternalForce`:
  - drag quadratico `½·ρ·Cd·A·|v−u|·(v−u)` con `u` la velocità del vento nel punto;
  - Archimede `−ρ_fluido·V·g` (V = volume dello scafo convesso, già calcolato in
    `_dyn_sim` per la massa);
  - il regime viscoso (miele) come `linearDamping`/`angularDamping` derivati da μ,
    non come una terza forza.
- **`A` è la trappola.** L'area frontale cambia mentre il pezzo ruzzola. Prendere
  l'area proiettata esatta a ogni step costa; prenderla costante fa volare una lastra
  come una sfera. Compromesso da adottare: proiezione dello scafo convesso su un
  numero fisso di direzioni (icosaedro, 42 direzioni) **precalcolata una volta per
  corpo**, poi interpolata sulla direzione relativa corrente. Costo ammortizzato ~0.
- **Il guardiano del sonno.** `_dyn_sim` esce dopo mezzo secondo di quiete. Un vento
  che parte in ritardo (o un getto che investe una pila già ferma) troverebbe la
  scena addormentata *prima* di cominciare, esattamente come succedeva a una teglia
  che si inclina lentamente. La soluzione esiste già ed è `_drive_until`: il vento
  deve estenderlo per tutta la sua durata, e `_t_max` va cresciuto di conseguenza.
- **Frontend: zero.** L'output restano keyframe per corpo, che `sceneBodyPose` già
  replica a 60 fps. Nessun nuovo `kind` di preview, nessun nuovo formato.

Livello 1 è la cosa da fare per prima: è la richiesta alla lettera, riusa il 100%
della pipeline esistente, e non dipende da niente in §2.

### Livello 2 — `WindTunnel`: il campo come geometria

- **`WindTunnel(shape, wind, fluid)`** → dominio dal bounding box scalato
  (default 1,5× a monte, 3,5× a valle, 0,9× lateralmente, come nella prova §2b),
  voxelizzazione §2a, D3Q19 con bounce-back.
- **Output 1: streamline come polilinee.** `mesh_extractor` emette già
  `{…, polylines: […]}` e il viewer le disegna: **altro frontend zero**. Le
  streamline si integrano nel campo con un RK2 a passo fisso da un reticolo di
  semi sul piano d'ingresso.
- **Output 2: un report** → `Panel`: Cd, area frontale, Reynolds, punto di
  ristagno, e il rapporto di bloccaggio del dominio (che è anche l'onestà
  richiesta da `PLAN_SIM.md` §C4 — sopra ~5% il Cd va letto come indicativo).
- **Le unità sono la parte che si sbaglia.** LBM lavora in unità reticolo e
  **non si accoppia alla velocità fisica, si accoppia al Reynolds**: si fissa
  Re = U·L/ν dal fluido scelto, si sceglie `u_lattice ≲ 0,1` (sopra, l'errore di
  compressibilità rovina tutto: è un metodo debolmente comprimibile, non
  incomprimibile) e da lì si ricava `tau`. Un `tau` troppo vicino a 0,5 diverge.
  Va scritto nel report, non nascosto.
- **Cacheabile**: nessun `random`, nessun `open()` — il nodo entra nella memo-cache
  senza avvelenare la propria discendenza. Da non rompere aggiungendo un seme.

### Livello 3 — particelle vive nel viewer (l'estetica del demo)

Il campo si "cuoce" una volta e le particelle le avanza il browser: è esattamente la
filosofia della *drag anticipation* (§6b) — bake esatto, replay locale.

- **Sul filo**, quantizzando a int16 e codificando base64:

  | griglia | JSON float | int16 + base64 |
  |---|---|---|
  | 48×24×24 | 567 KB | **216 KB** |
  | 64×32×32 | 1344 KB | 512 KB |
  | 96×48×48 | 4536 KB | 1728 KB |

  Cioè: 64×32×32 è il tetto ragionevole, e il JSON di float grezzi non è un'opzione.
- Serve **un `kind` di preview nuovo** (`Field`) e un path `THREE.Points`:
  `spheresFromData` costruisce una sfera da 144 triangoli **per punto**, va benissimo
  per qualche decina di punti di `Populate` e non regge migliaia di particelle.
- **Vincolo da non dimenticare: lo screenshot headless (§9) gira su SwiftShader,
  senza GPU**, dove già una scena con più corpi di vetro va in timeout con l'errore
  fuorviante "element not stable". Un sistema di particelle va tenuto sobrio, o
  disattivato quando `window.__noodleShot` è presente — altrimenti gli occhi
  dell'agente smettono di funzionare proprio sui grafi più interessanti.

## 4. Cosa resta esplicitamente fuori

**Il liquido a superficie libera** — l'acqua che sciaborda dentro il barattolo di
`threaded-jar-pour`. SPH o FLIP sono un altro ordine di costo (decine di migliaia di
particelle *per frame*, non un campo statico), e soprattutto non esiste un formato sul
filo per spedirle: la `Scene` a keyframe costa un corpo posabile per particella. Il
vento sui corpi rigidi e la galleria sono molto più abbordabili di quanto sembri;
questo no. Se servirà, è un progetto a sé, non un'estensione di questo.

## 5. Il demo Anthropic — cosa ci si può prendere

Il demo della campagna Opus 5 ("Opus 5 visualized the flow of air over aerodynamic
and non-aerodynamic objects") è pubblicato come artifact HTML singolo:
`https://assets.claude.ai/brand/artifacts/blog/opus/5-aeolus-demo.html`, linkato da
<https://www.anthropic.com/news/claude-opus-5>.

Analizzato: **Lattice-Boltzmann in shader WebGL**, collisione **MRT** (multiple
relaxation time — più stabile del BGK a Reynolds alti, ed è la cosa da copiare se il
BGK di §2b dovesse instabilizzarsi), ostacoli in **bounce-back**, colorazione per
**vorticità**, griglie ~192×76 → 500×200, quindi prevalentemente 2D.

**Non è codice riusabile**: bundle minificato da 1,5 MB, nessun repo, nessuna licenza
dichiarata — è un asset di brand. Il *metodo* è LBM da manuale, scienza pubblica, e si
reimplementa da zero senza toccare quel file: noodle resta MIT. Da lì si prendono le
scelte di progetto (MRT, vorticità come canale visivo, l'idea che il confronto
tozzo/affusolato è ciò che rende il demo leggibile), non le righe.

## 6. Ordine proposto

1. **Livello 1** (`Wind` + `medium` in `Drop`) — indipendente da tutto il resto.
2. **Livello 2** (`WindTunnel` → streamline + report) — nessun lavoro frontend.
3. **Livello 3** (campo cotto + particelle nel browser) — l'unico che tocca il viewer.

Nodi Tier 0 di `PLAN_SIM.md` §B: `Fluid` diventa il parametro `medium` del livello 1,
`Flow` diventa `Wind`, `DragEstimate` non serve più come nodo separato — il Cd vero
esce dal livello 2 e la stima analitica varrebbe meno del solver che costa 17 s.

---

## 7. Cosa è stato costruito davvero (2026-07-29)

Livelli 1 e 2. Nodi nuovi: **`Wind`** e **`WindTunnel`**, categoria `fluid`
(`catalog.py` §12d). `Drop` guadagna un socket `wind` e i parametri `medium`,
`drag`, `level`. Nessuna dipendenza nuova. Esempi: `examples/wind-drop.json` e
`examples/wind-tunnel.json`. Test: `tests/test_fluid.py` (35, puro-Python).

### 7a. Il difetto che c'era già: pybullet limitava le velocità a 100 mm/s

Il più importante di tutto, e non riguarda i fluidi. `createMultiBody` costruisce un
**btMultiBody**, che porta cablato `m_maxCoordinateVelocity = 100` unità/s — in
millimetri, **10 cm/s**. Misurato: un corpo lasciato cadere nel vuoto tiene
esattamente −100,0 mm/s per tutta la discesa invece di accelerare. La gravità era di
fatto spenta in ogni scena `collide` da sempre, e non si notava perché il timeline è
normalizzato: una caduta al rallentatore mostrata per intero si legge come plausibile.

`useMaximalCoordinates=True` lo rende un btRigidBody e il clamp sparisce: lo stesso
corpo dà −3270 / −6540 / −9810 mm/s a t = 1/3, 2/3, 1, cioè caduta libera alla cifra.
Verificato anche end-to-end: da 900 a 100 mm il sim dà **0,40 s** contro 0,40 s
analitici.

Serviva al drag più che a ogni altra cosa — va come v², e sotto il clamp non poteva
raggiungere un millesimo del peso del pezzo. Ma è una correzione di fisica, non una
feature, ed **è retroattiva su tutte le scene esistenti**:

- `container-tilt`, `drop-stack`, `threaded-jar-pour`, `drop-in-bowl`: rieseguiti e
  **guardati**, tutti corretti (la ciotola versa, le scatole si impilano).
- `galton-board`: regge la campana (centro più alto dei bordi) ma il fit gaussiano
  scende da **+0,81 a +0,47**. Le palline ora arrivano ai chiodi alla velocità vera e
  rimbalzano di più. Provate quattro combinazioni material/grip: quella attuale
  (`wood`, 0.15) resta la migliore, quindi **non è una taratura sbagliata, è la
  fisica corretta**. Se lo si vuole riportare a +0,8 bisogna ritarare passo dei
  chiodi o altezza di caduta — è una scelta di contenuto, non l'ho fatta.

### 7b. Le tre cose che il modello ha sbagliato prima di funzionare

- **La silhouette non fa ruotare niente.** Il primo `_body_aero` costruiva la tabella
  proiettando lo scafo: le aree venivano giuste (rapporto 20,0 per una lastra, 1,00
  per una sfera) ma **tutti i centri di pressione uscivano esattamente zero**, perché
  per un corpo centralmente simmetrico il centroide della silhouette *è* la proiezione
  del baricentro. Una lastra nel vento non si sarebbe mai ribaltata. La tabella è ora
  integrata sulle **facce** dello scafo (panel drag newtoniano): stesso costo, e in più
  la forza non è più parallela al vento — una superficie inclinata riceve una spinta
  laterale pari a 0,90× quella lungo il flusso a 45°, che è ciò che fa volare una
  carta. Verificato: area = silhouette esatta (cubo 400,00), cono di traverso genera
  coppia, cono allineato al flusso ne genera zero.
- **Un gas non ha un pelo libero.** Avevo fatto scalare il drag con la frazione
  sommersa. Con l'aria e la superficie a 0 il corpo è sempre "fuori dal fluido" e la
  resistenza si annullava: **un vento a 12 m/s spostava una lastra di un millimetro**.
  `_MEDIUM` porta ora un flag `is_liquid`: un liquido riempie fino a `level`, un gas
  riempie tutto.
- **Un liquido senza superficie è un oceano senza cima.** Prima del flag, un cubo di
  legno in acqua non galleggiava: saliva a **z=858** e continuava. Con `level` si ferma
  al 63% sommerso contro il 60% teorico (ρ_pezzo/ρ_fluido; il 3% è la linearizzazione
  sulla sfera equivalente).

### 7c. Il Cd: il cancello NON è passato come previsto

`PLAN_FLUID.md` lo trattava come una riga del report. Non lo è.

1. Prima versione dello scambio di quantità di moto: **Cd = −3,28**, segno sbagliato.
2. Corretta la formula: **Cd ≈ 20** e ordine invertito.
3. Il vero bug era nel solver, non nella forza: `np.roll(A, s)[x]` è `A[x−s]`, quindi
   il vicino in `x + c[q]` è `roll(solid, −c[q])`. Con il segno sbagliato il set di
   link di bounce-back è **specchiato**, le popolazioni riflesse finiscono sulle celle
   sbagliate e **la massa viene distrutta invece che rimbalzata**: la densità cala dal
   primo passo, diventa negativa al sesto, e il Cd esce `nan`. Sembrava un problema di
   stabilità — diverge a *ogni* tau, anche 0,70 — e non lo era.
4. Corretto il segno, la sfera dà **Cd 1,37 a Re 159** contro **0,89** di
   Schiller-Naumann: rapporto 1,54, **entro il fattore 2** del cancello.
5. **Ma l'ordine goccia < sfera < cubo continua a fallire**, e il motivo non è il
   solver: ogni corpo finisce a un **Reynolds diverso** (86, 159, 183), perché `dx`
   deriva dall'ingombro massimo e `L_ref` dall'area frontale. A questi Re il Cd va
   come ~24/Re, quindi un corpo lungo e sottile prende un Cd alto *per il Reynolds*,
   non per la forma. **Confrontare forme a Re diversi non significa nulla.**

**Il cancello che passa è quello appaiato**: lo *stesso* corpo girato nei due versi —
stessa griglia, stesso `dx`, stesso Re. Lì il solver risponde bene, e mi ha anche
corretto: coda a valle **1,157** contro punta a monte **1,314**. Avevo l'aspettativa
sbagliata — una goccia è tonda davanti e appuntita dietro, e un cono col vertice
controvento ha una base piatta a valle che stacca una scia enorme.

Quindi il Cd **c'è**, ma il report lo stampa accanto al Reynolds con sei righe che
dicono di non confrontarlo fra forme a Re diversi. Non è la strada 5 del piano
(spedire senza Cd) e non è nemmeno la 3: è un numero onesto con il suo dominio di
validità scritto sopra. Chi vuole classificare due forme gira un corpo.

### 7d. Quel che resta aperto

- **Risoluzione contro discriminazione.** A `draft` il corpo è ~11 celle di lato e il
  solver non distingue un cono da un cilindro (1,62 contro 1,56). A `normal` sì
  (1,314 / 1,157). Legare `dx` all'area frontale invece che all'ingombro renderebbe
  il Re uguale per tutti — ma per un corpo lungo e sottile fa esplodere il conto delle
  celle. Non risolto.
- **Il bloccaggio** resta 6–8% per corpi tozzi anche col dominio a ±2L: la
  scalinatura dei voxel gonfia l'area frontale. Il report lo dice.
- **MRT** (`§5`) non è servito: il BGK, col bounce-back giusto, è stabile. Resta la
  carta da giocare se si vorrà salire di Reynolds.
- **Livello 3** (particelle nel browser) non fatto, per scelta.
