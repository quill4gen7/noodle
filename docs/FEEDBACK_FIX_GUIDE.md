# Feedback → fix → PR — guida per l'agente di coding

> Guida operativa per un **agente di coding esterno** (es. Claude Code via MCP,
> vedi `AGENTS.md`) che raccoglie un report di feedback salvato dall'app, lo fixa
> in autonomia **senza rompere nulla**, e propone una PR. La skill di ingresso è
> `.claude/skills/feedback-fix/SKILL.md`; questo file è la procedura completa.
>
> Principio guida: **ogni modifica deve essere reversibile.** In qualsiasi momento
> devi poter tornare a uno stato sicuro con un comando git.

---

## 1. Dove sono i report

I report vivono in **`feedback/<id>/`** (cartella per report, `id` =
`<timestamp-UTC>-<slug>`). La directory è **gitignored** e montata read-write
(`docker-compose.yml`), quindi è locale all'utente e **non va mai committata**.

Ogni report contiene:

| File | Contenuto |
|---|---|
| `report.md` | Report leggibile: severità, messaggio, contesto tecnico, come riprodurre, e un blocco "Per l'agente di coding". |
| `report.json` | Gli stessi dati in forma strutturata (per tooling). |
| `graph.snapshot.json` | Copia del grafo al momento del report (riproducibilità). Presente solo se l'utente l'ha allegato. |
| `backend.log` | Ultime righe di log backend (uvicorn + worker). Presente solo se allegato. |

Parti dal report **più recente** se non te ne viene indicato uno specifico.
`GET /api/feedback` ne restituisce la lista (newest first).

## 2. Git in sicurezza / tornare a uno stato sicuro

**Prima di toccare codice:**

1. **Working tree pulito.** `git status` deve essere clean. Se ci sono modifiche
   pendenti non tue, fermati e chiedi: non mescolare il fix con altro.
2. **Branch personale dell'utente + branch di PR separati.** Strategia a due livelli
   che tiene insieme tutto il lavoro locale dell'utente **e** mantiene le PR pulite:
   - **Branch personale** (`user/<nome>`, es. `user/quill`): branch a vita lunga che
     **aggrega tutte le modifiche dell'utente**. È il suo "main locale" — qui
     converge tutto ciò che vuole tenere, anche fix non ancora proposti upstream.
   - **Branch di PR dedicato** per ogni intervento, tagliato dal personale (o da
     `main`): `fix/feedback-<id>` per un report, `feat/<slug>` per una feature. Una
     PR = un branch = un argomento, così la review upstream resta isolata e singola.
   ```bash
   git switch user/quill                 # o: git switch -c user/quill main
   git switch -c fix/feedback-<id>       # branch di PR, parte dal personale
   ```
   A fine lavoro, allinea il personale (merge/cherry-pick del branch di PR) così
   l'utente mantiene insieme tutte le sue modifiche, mentre la PR vive sul suo branch.
3. **Tag/branch di salvataggio prima di modifiche delicate** (transpiler, executor,
   worker, formato grafo): `git tag safe/before-<id>` — un punto di ritorno esplicito.

**Durante:**

- **Commit di checkpoint frequenti**, piccoli e descrittivi. Ogni checkpoint è un
  punto a cui puoi tornare.
- Non committare mai: la dir `feedback/`, segreti/chiavi, `projects/` dell'utente,
  `*.stl`/`*.step` generati (già in `.gitignore`).

**Tornare a uno stato sicuro (cheat-sheet):**

| Situazione | Comando |
|---|---|
| Buttare le modifiche non committate del working tree | `git restore .` (o `git stash` per metterle da parte) |
| Tornare all'ultimo checkpoint committato | `git reset --hard HEAD` |
| Tornare al punto pre-modifica salvato | `git reset --hard safe/before-<id>` |
| Annullare un commit ma tenere le modifiche in working tree | `git reset --soft HEAD~1` |
| Abbandonare tutto e tornare a main | `git switch main && git branch -D fix/feedback-<id>` |

Se a un certo punto non sei sicuro di cosa hai cambiato: `git diff main...HEAD`.

## 3. Modifiche delicate — regole della repo

`CLAUDE.md` §6 è la **fonte di verità** per come applicare e verificare le
modifiche. In sintesi, ciò che rompe più spesso se ignorato:

- **Backend (Python):** dopo aver cambiato `server.py` / `cad_nodes/*` →
  `docker restart noodle` (il processo cache-a gli import; il mount read-only
  da solo non basta). `CLAUDE.md` §1/§6.
- **Frontend (`webui/*.html`):** hard-refresh del browser (Ctrl+Shift+R), file
  statico cache-ato. `CLAUDE.md` §6.
- **Verifica veloce senza Docker:** usa il venv host il container (`docker exec -i noodle python`, CLAUDE.md §2) per transpilare
  ed eseguire un grafo prima di riavviare il container. `CLAUDE.md` §2.
- **Wire types in due posti:** se tocchi i tipi di wire, tieni in sync
  `cad_nodes/catalog.py` `WIRE_COMPATIBLE` **e** `webui/nodes.html` `INPUT_ACCEPTS`.
  `CLAUDE.md` §5.
- **Nuove capability dell'engine** vanno in `cad_nodes/api.py` così UI, MCP e
  copilot le ottengono tutte. `CLAUDE.md` §6.
- **`projects/` è root-owned** (il container scrive come root): modifica i grafi via
  API/UI, non a mano. `CLAUDE.md` §6.
- **Test:** `python -m pytest tests/ -v` (pure-Python, no build123d). Devono restare
  verdi; aggiungine uno che copra il bug se sensato. `CLAUDE.md` §8.

Riproduci il bug **prima** del fix e ri-verifica **dopo** (stesso grafo snapshot).

## 4. Comporre la PR

Apri la PR **dal branch di PR dedicato** (vedi §2: mai dal branch personale, che
aggrega più cose) usando il template `.github/PULL_REQUEST_TEMPLATE.md`. Una PR
copre un solo argomento; tieni il personale dell'utente come raccolta a parte.
Includi:

- **Report di origine:** l'`id` del feedback (`feedback/<id>`). **Non** committare
  i file del report; riferiscili soltanto (eventualmente cita il messaggio).
- **Come riprodurre:** passi minimi; allega il grafo snapshot se serve a chi revisiona.
- **Cosa è cambiato e perché:** il bug, la causa, il fix.
- **Test eseguiti:** comandi e esito (`pytest`, verifica host container, restart).
- **Before/after:** se è un cambiamento di comportamento/geometria.

## 5. Disclosure AI — OBBLIGATORIA

Ogni PR generata con assistenza AI **deve sempre dichiarare**:

1. **Quali agenti di coding** sono stati usati (es. Claude Code, Cursor, …).
2. **Quali modelli** (es. `claude-opus-4-8`).
3. Facoltativo ma incoraggiato: **la sessione/transcript dell'agente** —
   **solo dopo averla ripulita** da dati sensibili (chiavi/API, percorsi privati,
   contenuti personali, segreti dei progetti utente). Se non puoi garantire che sia
   pulita, **non allegarla**: riassumi a parole l'approccio.

Nota: ci si riferisce all'**agente di coding esterno** che produce il fix — **non**
al copilot in-app, che per policy non modifica l'app (`CLAUDE.md` §7). La sezione
"Disclosure AI" del template è un campo da compilare, non opzionale.
