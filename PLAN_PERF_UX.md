# Piano prestazioni e UX

## Obiettivo e ordine
Correggere prima perdita di dati e risultati incoerenti, poi ridurre lavoro inutile.
Non cambiare semantica CAD o qualità degli export per inseguire FPS.

## 1 — Affidabilità (priorità critica)
- [x] Autosave: debounce su modifiche reali, scrittore serializzato, errore persistente e retry esplicito.
- [x] Operazioni UI vincolate a progetto/sessione e snapshot; nessun run/export dopo un save fallito.
- [x] Cambio/creazione/eliminazione progetto transazionali; cancellare richieste obsolete e pulire anteprima.
- [x] File di esecuzione isolati per job, risultati pubblicati atomicamente; preservare cwd del progetto per asset/export relativi.
- [x] Scritture atomiche dei grafi condivise tra REST e GraphStore.

## 2 — Esecuzione Live e feedback
- [x] Una esecuzione client alla volta, una sola modifica pendente (latest wins).
- [x] Attesa worker limitata, stato busy/queue e cancellazione cooperativa dei job in attesa.
- [x] Progress per run, fasi coda/calcolo/anteprima; evitare riletture duplicate.
- [x] Export con salvataggio verificato, formato e stato/errori; riuso worker caldo senza cambiare precisione.

## 3 — Rendering e avvio
- [x] Rendering su richiesta con invalidazione esplicita per camera, gizmo, sketch, replay e immagini; sospensione a tab nascosta.
- [x] Riuso geometrie invariate via hash backend; aggiornamento materiali senza ricalcolare mesh/normali.
- [x] Richieste bootstrap indipendenti parallele con timeout/retry; guida per canvas vuoto.
- [x] Accessibilità delle tab/modali e miglioramento delle azioni principali.
- [x] Dipendenze frontend disponibili localmente con licenze e versioni fissate.

## 4 — Verifica e follow-up misurato
- [x] Test deterministici per timer autosave, salvataggi sovrapposti, sessioni obsolete, coalescing Live.
- [x] Test Python con worker simulato: isolamento job, timeout coda, cancellazione, pubblicazione atomica.
- [x] Test viewer con browser reale se disponibile; suite Python, sintassi JS e lint.
- [x] Documentare risultati e limiti ambientali.

Da misurare prima di un cambio di protocollo: dimensione payload, JSON encode/decode,
tempo upload GPU, memoria cache e draw call. Buffer binari, Web Worker, cache con budget
in byte e worker multipli sono una fase successiva, non modifiche speculative.
Una traduzione completa richiede un catalogo i18n: evitare sostituzioni parziali di
centinaia di etichette. La modularizzazione totale dell'editor è separata dai bugfix.

## Criteri di accettazione
- Una modifica con Live spento viene salvata dopo quiete, anche lasciando aperta la pagina.
- Save fallito non esegue/esporta una versione vecchia né cancella il draft.
- Due run concorrenti non condividono script/view/progress, anche nello stesso progetto.
- Cambiare progetto durante un run non può riportare la geometria precedente.
- Una raffica di richieste Live mantiene al massimo un run attivo e uno pendente.
- A camera/scene ferme non si continua a renderizzare; replay e gizmo restano visibili.
- Cambiare solo colore non ricrea la geometria.

## Verifica (questa sessione)
- `python -m pytest tests/` → 400+ passed, 44 skipped (perf opt-in).
- `node --test tests/ui/*.cjs` → 6/6.
- `docker exec -i noodle python - < tests/ui/smoke.py` → CAD reale, riuso GPU, autosave, rendering idle, risposte stale, save fallito, focus modale. Chromium nello stesso container; progetti usa-e-getta.
- Warm box: ~3.0s cold → ~0.03s memo; export STEP sul worker caldo ~0.04s.
- Viewport idle: 0 frame extra dopo 500ms; una mutazione camera lo riaccende.
- Host senza ruff; lint in CI (`ruff-action`).

## Unione con view-generations (2/10)
Lavoro del 25/9 sul mini PC, portato sopra `feat/view-generations` (viewer delle generazioni,
API per agenti, live sync) in sei pezzi, ognuno con i suoi test.
- I salvataggi in coda convivono con la live sync: ogni scrittura dichiara la versione di base,
  un 409 fa il merge e risalva dentro lo stesso passo della coda (`tests/ui/session.cjs`).
- `/view` chiede un frame al viewer a ogni posa della timeline e quando nasconde pezzi
  (il viewer ora disegna solo su richiesta).
- Il bake non ri-aggancia più le forme della cache: dopo un bake l'export STEP del risultato falliva.
- Anche `/view` carica three.js da `webui/vendor/`.
