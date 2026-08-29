# PLAN_SIM — simulazione strutturale FDM e fluidodinamica (note)

Stato: **note di design, nessuna implementazione**. Discussione 2026-07-02.
Decisione: si punta al **Tier 1 per entrambe** le famiglie (FEA per la rottura,
LBM per i fluidi); i Tier 0 restano utili come primi nodi del vocabolario e
possono precedere i solver.

## A. Rottura pezzi stampati FDM

Fisica chiave: l'FDM è **anisotropo** — la direzione debole è l'adesione tra
layer (Z di stampa), ~40–70% della resistenza XY, funzione di layer height,
temperatura e materiale.

- **Tier 0 (giorni)** — verifica analitica live, pura data-entry in catalog:
  `Material` (preset PLA/PETG/ABS/… → E, σy, ρ), `PrintSettings` (layer height,
  piano di stampa — riusa PlaneNormal —, infill % → knockdown interlayer da
  tabella empirica + omogeneizzazione infill Gibson-Ashby E∝ρ²),
  `StressCheck` (Section esistente + carico → σ flessione/trazione vs σy
  direzionale → **fattore di sicurezza sullo slider, in tempo reale**).
- **Tier 1 (scelto, 1–2 settimane)** — FEA statica vera:
  build123d → STEP → **Gmsh** (mesh tetra) → **CalculiX** (ccx, apt) →
  von Mises color-mapped nel viewer. Vincoli/carichi assegnati con i nodi
  `SelectFace` GIÀ esistenti (`Constraint` = facce fisse, `Load` = forza/
  pressione). Materiale trasversalmente isotropo orientato col piano di stampa.
- **Tier 2 (scartato)** — propagazione cricche/XFEM: livello ricerca, non
  ripaga (dispersione sperimentale FDM enorme). Il deliverable giusto è la
  mappa del safety factor, non il filmato della cricca.

## B. Aero/idrodinamica (bounding box + ugello + fluido)

> **Superato da `PLAN_FLUID.md` (2026-07-27), che lo corregge su un punto.**
> Misurato: un D3Q19 in **numpy puro, dentro il worker attuale**, fa 19 ms/step su
> 96×32×32 → galleria completa in ~17 s. Quindi per la v1 **niente FluidX3D, niente
> GPU, niente immagine `noodle-sim` separata** (§C3 non si applica alla galleria) e
> **niente coda asincrona** (§C1 resta un prerequisito della sola FEA). Il vero
> vincolo è un altro e non era previsto qui: il costo scala in modo superlineare
> oltre ~100k celle, e la *voxelizzazione* col winding number costerebbe più del
> solver (8,65 s contro 0,017 s). Dettagli e misure in `PLAN_FLUID.md`.

- **Tier 0 (giorni)** — `Fluid` (aria/acqua/olio → ρ, μ), `Flow` (velocità,
  direzione), `DragEstimate` (area frontale proiettata + Cd di classe →
  forza, Reynolds).
- **Tier 1 (scelto, 2–4 settimane)** — galleria del vento **Lattice-Boltzmann**:
  voxelizza l'STL direttamente, **niente meshing** (l'80% del dolore del CFD
  classico). Nodi: `WindTunnel` (dominio = BoundingBox scalata), `Nozzle`
  (inlet circolare posizionato con un piano: diametro + velocità — il getto
  che investe il pezzo), `Fluid`. Output: drag/lift + slice del campo di
  velocità nel viewer. Backend: **FluidX3D** (OpenCL, GPU) pilotato come
  servizio, oppure mini-LBM numpy interno per domini piccoli (~64³) senza
  dipendenze.
- **Tier 2 (rimandato)** — OpenFOAM come backend alternativo dello stesso nodo
  `WindTunnel`, solo se servirà fedeltà da certificazione.

## C. Impatti trasversali (prerequisiti condivisi)

1. **Job asincroni**: l'executor è sincrono con timeout; le sim richiedono
   coda + progresso + cache dei risultati (hash del sottografo a monte).
2. **Viewer**: campi scalari color-mapped sulla mesh + legenda (estensione di
   `renderPreviews`).
3. **Docker**: Gmsh+CalculiX pesano, FluidX3D vuole GPU → immagine `noodle-sim`
   opzionale / profilo compose separato; l'immagine base resta snella.
4. **Onestà ingegneristica**: risultati FDM etichettati come indicativi
   (variabilità di stampa); meglio un intervallo che una cifra singola.

## Ordine proposto

1. Infrastruttura job asincroni (serve a entrambe).
2. FEA CalculiX (valore ingegneristico più alto per chi stampa).
3. Galleria LBM (valore insight/demo più alto).
4. (opzionale, in qualsiasi momento) nodi Tier 0 come vocabolario e fallback.
