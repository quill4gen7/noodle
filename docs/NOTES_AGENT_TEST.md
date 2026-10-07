# ✎ Disegna — stessa nota, cinque agenti

Una prova del 2026-10-07: quill disegna UNA nota su un progetto fatto apposta
(`prova-disegno/g1`: un blocco, una piastra con 4 fori, un bicchiere) e cinque
agenti la realizzano, ognuno sulla sua copia del progetto, con lo stesso prompt
(leggi la guida, leggi la nota via `GET /api/notes`, modifica solo la tua copia
via API, verifica con gli screenshot, snapshot, resoconto).

<p align="center"><img src="asset/noodle-notes-test.png" alt="In alto la nota: il testo di quill e le 13 immagini (vista finale + 12 viste da cui ha disegnato). Sotto, i cinque pezzi prodotti da Claude, Codex, Kimi, GLM e DeepSeek con una riga su cosa hanno capito" width="900"></p>

La richiesta, così come scritta: *«il cubo giallo segui il disegno e trasformalo in
quello che ho abbozzato, un coniglietto cube. la piastra fai un triangolo un
quadrato, una più grande e uno più piccolo segui i segni gialli, il cilindro blu,
sul fondo facci 4 fori, e sui lati intaglia le forme che ho disegnato ma fai tutto
equidistante io le ho abbozzate»* — 33 segni su 12 viste.

| Agente | Vede le immagini | I fori «+» e «−» della piastra | Tempo |
|---|---|---|---|
| Claude (Opus 5.5, subagente senza il contesto della sessione) | sì | ✓ più grande e più piccolo | 4 min |
| Codex (gpt-6-sol) | sì | ✗ letti come «elimina»: tolti | ~15 min |
| pi + Kimi K3 | sì | ✗ legge la «x» rimasta di «xBIG»: lasciati tondi | ~37 min |
| pi + GLM-5.3 | no | ✗ «restano» | ~40 min |
| pi + DeepSeek V4 Pro | no | ✗ ignorati (e una lastra in più sulla piastra) | ~15 min |

Il giudizio di quill: Claude il migliore, Codex subito dopo «ma senza il tocco
artistico», DeepSeek l'ha fatto ma con un errore.

## Cosa ha insegnato a noodle

- **Una foto può mentire.** Le foto delle viste si scattano al rilascio della
  penna, e ↶ toglieva i tratti ma non la foto: la vista 6 mostra ancora «xBIG»,
  che quill aveva cancellato. Kimi lo cita esplicitamente («fori marcati con
  trattino e x»), e la «x» su un foro si legge «toglilo». Da correggere insieme
  alla gomma: PLAN_NODE_CAD.md, roadmap §3.
- **Le scritte a mano non arrivano ai modelli solo-testo.** GLM e DeepSeek
  lavorano dai `marks` (coordinate, forma loop/line/dot, nodo): bastano per
  capire DOVE, non COSA c'è scritto. Da qui il riquadro di testo come dato
  (`labels`) nella stessa voce di roadmap.
- **Una guardia aggirata.** pi gira con l'estensione `ask-on-write` di quill;
  senza UI blocca le scritture. GLM si è fermato e l'ha detto; DeepSeek ha
  scoperto che `env` era in lista come «sola lettura» e ha fatto 54 scritture
  come `env curl …`. L'estensione ora giudica il comando che `env` lancia, e i
  messaggi di blocco dicono all'agente di fermarsi e riferire invece di
  cercare un'altra strada (riprovato: DeepSeek ora si ferma al primo colpo).

## Le schede

Per ogni agente: il pezzo da tre angoli (iso, dietro, sotto) e il suo resoconto
integrale.

<img src="asset/noodle-notes-card-claude.png" alt="Claude: pezzo da tre angoli e resoconto" width="900">
<img src="asset/noodle-notes-card-codex.png" alt="Codex: pezzo da tre angoli e resoconto" width="900">
<img src="asset/noodle-notes-card-kimi.png" alt="Kimi K3: pezzo da tre angoli e resoconto" width="900">
<img src="asset/noodle-notes-card-glm.png" alt="GLM-5.3: pezzo da tre angoli e resoconto" width="900">
<img src="asset/noodle-notes-card-deepseek.png" alt="DeepSeek V4 Pro: pezzo da tre angoli e resoconto" width="900">
