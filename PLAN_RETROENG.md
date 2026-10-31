# PLAN_RETROENG — retro-ingegneria STL/STEP/gcode → grafo procedurale (note)

Stato: **fase 2 implementata** (2026-10-31) — `cad_nodes/lens.py`: `cad_lens`
(vista prospettica + piani di taglio + contorni veri proiettati sul pezzo,
accanto alle sezioni 2D quotate; sweep fino a 12 tagli) e `cad_views` (N
angoli su un foglio con camera e terna XYZ); montaggio con lo stesso Chromium
degli screenshot, nessuna libreria immagini. Sezioni mesh robuste sugli scan
(saldatura, cammino per spigoli, ricucitura dei buchi, rumore scartato).
Lezione del caso `reddit-coscia-scan`: un loft di sezioni campionate copia la
forma ma non è un retroeng — serve il metodo del progettista (datum, patch,
sformi), vedi `cad_help(topic="retroeng")` punto 2c.

**Fasi 1 e 3 implementate** (2026-07-03) — `cad_nodes/slice_summary.py`:
`slice_summary` (STEP esatto + STL con arc-fitting) e `section_outline` (il
"microscopio"), esposti su HTTP / MCP / copilot via `cad_nodes.api`; nodo
`ToAgent` per taggare i file da retroingegnerizzare (label+data+provenienza,
indice in `api.agent_tags`). Validato su due pezzi reali: `supportmotory.step`
ricostruito a Δvolume 0.05% e `cap.stl` (mesh 59k tri) a +2.2% — entrambi con
soli nodi di catalogo, grafi `retro_nodes` e `retromy`. Restano: fase 2
(contact-sheet vision), stripper gcode, `cad_compare` numerico (un confronto
di AREE per sezione si è già dimostrato il debugger giusto per i residui).
Nota mesh: il chaining di una singola sezione può perdere loop vicino a
superfici tangenti — confermare sempre con sezioni vicine o con le aree.
Discussione originale 2026-07-02; fattibilità validata (vedi §Esperimento).

## Idea

Metodo **universale** che conta sull'intelligenza del modello (testo +
multimodalità), NON su rilevatori geometrici specializzati: si affetta la
forma in pile di sezioni sui 3 piani (come uno slicer, ma su XYZ), si comprime
ogni sezione in testo simbolico, e si lascia che l'LLM (il copilot) ricostruisca
forma e *intento procedurale* — quali nodi usare, quali valori diventano
parametri — guidato dall'indicazione dell'utente.

## Il tool centrale: `cad_slice_summary(shape, n_per_asse)`

Un solo tool in `cad_nodes.api` (→ gratis su UI, MCP e copilot):

1. **Sezioni sui 3 piani** (default ~10 per asse; adattivo: infittisci dove le
   sezioni cambiano).
2. **Compressione simbolica per sezione**: dal B-Rep le sezioni escono già
   esatte — `circle r=3 @(x,y)`, `rect 40x30`, `poly(n edges)` come fallback.
   Ogni loop esterno con i suoi fori interni.
3. **Dedup con intervalli**: "z=−3 … z=4.5: identica" — è insieme compressione
   e semantica (sezione costante = estrusione; gli intervalli danno le altezze).
4. **Checksum**: bbox + volume.
5. Opzionale (via multimodale): contact-sheet PNG delle sezioni + 3 viste
   ortogonali (Section/proiezioni → ExportSVG, che esiste già) — il "disegno
   tecnico" è il formato che gli ingegneri hanno inventato apposta per
   comunicare il 3D in 2D, ed è l'input vision ideale.

### Ingressi supportati dalla stessa pipeline

- **STEP**: sezioni simboliche esatte, il caso migliore.
- **STL**: sezioni = polilinee → serve **arc-fitting** (polilinea ≈ cerchio/arco).
- **gcode**: È già una pila Z di contorni (le istruzioni di fabbricazione) →
  stripper (via travel/infill/skirt, tieni i perimetri chiusi) + arc-fitting +
  dedup layer identici → stesso formato. Il gcode grezzo non è dato all'LLM
  (100k righe di coordinate affogano il contesto).

## Il loop di verifica usa la STESSA rappresentazione

Il copilot: affetta il target → ragiona → costruisce il grafo → esegue →
**ri-affetta il proprio risultato** → confronta i due riassunti come TESTO
("target: boss r=8; mio: r=6 → correggo"). Confrontare testi è ciò che un LLM
fa nativamente: percezione e verifica con lo stesso strumento, senza metriche
geometriche esterne. (Un `cad_compare` numerico — Δvolume, Hausdorff — resta
utile come conferma finale, ma non è il motore del loop.)

## Proceduralizzazione (la parte difficile, delegata al modello)

- Regole di default nel system prompt: sezione costante → Extrude; N cerchi
  uguali disposti regolarmente → ArrayLinear/ArrayPolar con `count` slider,
  non copie; raccordi piccoli → nodo Fillet a valle, non geometria modellata;
  dimensioni d'ingombro → slider con range sensati; conteggi → int slider.
- **L'intento dell'utente entra nel prompt** ("parametrizza numero fori e
  diametro; lo spessore resta fisso") e vince sulle regole di default.

## Esperimento di validazione (fatto, 2026-07-02)

Pezzo di prova nel container: piastra 40×30×10 + 4 fori d'angolo r3 passanti
(pattern 30×20) + boss r8 h8 + foro centrale r4 passante tutto.
11 sezioni Z + 5 X + 5 Y compresse in ~15 righe di testo simbolico →
**forma e grafo procedurale ricostruiti correttamente dalla sola pila Z**
(4 righe informative dopo il dedup); pile X/Y come conferma. Le sezioni
oblique sui fori degradano a `poly(6: LINE)` — è lì che serve la via vision.

## Limiti onesti

- Forme organiche/scansioni: il simbolico degrada (`poly(400 edges)`) → via
  vision + ricostruzione a loft di sezioni campionate (comunque procedurale).
- Feature più sottili del passo di sezione → slicing adattivo.

## Fasi

1. `cad_slice_summary` (STEP/solidi interni) + regole prompt + loop
   ri-affetta-e-confronta nel copilot. L'esperimento dice che basta questo
   per i pezzi 2.5D/meccanici.
2. Contact-sheet PNG + 3 viste per la via multimodale.
3. Arc-fitting per STL; stripper gcode.
4. `cad_compare` numerico come sigillo finale (Δvolume/Hausdorff).
